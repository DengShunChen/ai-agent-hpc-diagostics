"""Log tools over the incident's saved, de-identified log index (read-only, saved data only).

get_log_lines: search one log source for lines containing all keywords.
get_log_digest: the warning and error lines on the failed job's own nodes while it ran (fixed terms, no model).
analyze_logs: a fixed set of analyses across all saved log sources. The model chooses an operation
and plain keywords; this module runs it. No expressions, no code, no paths: the model composes an
analysis, it never writes one that runs.

The logs are the de-identified index written by the importer (test_data/logs/<id>/<source>.json).
A line matches when it contains every keyword (case-insensitive) in its node name or text: plain
substring search, no regular expressions. Results are capped so the model always sees whole lines,
and the total count tells it to narrow the search.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.safety.policy import Operation
from hpc_agent.tools.gateway import ToolResult, ToolSpec, ToolStatus

LOG_SOURCES = ("slurmctld", "slurmd", "pjm", "syslog")
MAX_SHOWN_CHARS = 2400  # stays under what the harness passes to the model (runtime/tools.py)
MAX_KEYWORDS = 6


def search(adapter: ReadOnlyDataAdapter, incident_id: str, source: str, keywords: str) -> ToolResult:
    segments = ("test_data", "logs", incident_id, f"{source}.json")
    if not adapter.exists(*segments):
        return ToolResult(tool="get_log_lines", status=ToolStatus.NO_DATA)
    data = adapter.read_json(*segments)
    terms = [t.lower() for t in keywords.split()][:MAX_KEYWORDS]
    matches = [line for line in data.get("lines", [])
               if all(t in f"{line.get('node') or ''} {line.get('text', '')}".lower() for t in terms)]
    shown, size = [], 0
    for line in matches:
        size += len(json.dumps(line))
        if size > MAX_SHOWN_CHARS:
            break
        shown.append(line)
    return ToolResult(tool="get_log_lines", status=ToolStatus.OK, source="/".join(segments), data={
        "source": source, "keywords": " ".join(terms), "total_matches": len(matches),
        "shown": len(shown), "lines": shown,
    })


ANALYSES = ("count_by_node", "timeline", "near", "nodes_with_both")
MAX_WINDOW_SECONDS = 3600


def _matches(line: dict, terms: list[str]) -> bool:
    return all(t in f"{line.get('node') or ''} {line.get('text', '')}".lower() for t in terms)


def _terms(keywords: str) -> list[str]:
    return [t.lower() for t in keywords.split()][:MAX_KEYWORDS]


def _when(line: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(line.get("timestamp")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _all_lines(adapter: ReadOnlyDataAdapter, incident_id: str) -> list[dict]:
    """Every saved log line of the incident, tagged with its source ("log"), oldest first."""
    out = []
    for source in LOG_SOURCES:
        segments = ("test_data", "logs", incident_id, f"{source}.json")
        if adapter.exists(*segments):
            out += [{"log": source, **line} for line in adapter.read_json(*segments).get("lines", [])]
    return sorted((x for x in out if _when(x)), key=lambda x: (_when(x), x["log"], x["line"]))


def _cap(rows: list[dict]) -> list[dict]:
    shown, size = [], 0
    for row in rows:
        size += len(json.dumps(row))
        if size > MAX_SHOWN_CHARS:
            break
        shown.append(row)
    return shown


def _where(line: dict) -> dict:
    """The identifiers of a line: its source, node (job manager lines have none) and line number."""
    return {"log": line["log"], **({"node": line["node"]} if line.get("node") else {}), "line": line["line"]}


def analyze(adapter: ReadOnlyDataAdapter, incident_id: str, operation: str, keywords: str, also_keywords: str,
            window_seconds: str) -> ToolResult:
    lines = _all_lines(adapter, incident_id)
    if not lines:
        return ToolResult(tool="analyze_logs", status=ToolStatus.NO_DATA)
    terms, also = _terms(keywords), _terms(also_keywords)
    window = timedelta(seconds=min(max(int(window_seconds), 1), MAX_WINDOW_SECONDS))
    hits = [x for x in lines if _matches(x, terms)]
    rows: list[dict] = []
    if operation == "count_by_node":
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for x in hits:
            groups[(x["log"], x.get("node"))].append(x)
        for (log, node), group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            rows.append({"match": " ".join(terms), "log": log, **({"node": node} if node else {}), "count": len(group),
                         "first_seen": group[0]["timestamp"], "first_line": group[0]["line"],
                         "last_seen": group[-1]["timestamp"]})
    elif operation == "timeline":
        buckets: dict[datetime, list[dict]] = defaultdict(list)
        for x in hits:
            when = _when(x)
            start = datetime.fromtimestamp(when.timestamp() // window.total_seconds() * window.total_seconds(), when.tzinfo)
            buckets[start].append(x)
        for start, group in sorted(buckets.items()):
            rows.append({"match": " ".join(terms), "bucket": start.isoformat(), "count": len(group),
                         "nodes": len({x.get("node") for x in group if x.get("node")}),
                         "first_line": group[0]["line"], "first_log": group[0]["log"]})
    elif operation == "near":
        anchors = hits
        for x in (y for y in lines if _matches(y, also)):
            before = [a for a in anchors if timedelta(0) <= _when(x) - _when(a) <= window and a is not x]
            if before:
                a = before[-1]
                rows.append({**_where(x), "timestamp": x["timestamp"], "text": x["text"],
                             "seconds_after_anchor": round((_when(x) - _when(a)).total_seconds(), 3),
                             "anchor_log": a["log"], "anchor_line": a["line"],
                             "same_node": bool(x.get("node")) and x.get("node") == a.get("node")})
    elif operation == "nodes_with_both":
        first: dict[str, dict] = {}
        for x in hits:
            if x.get("node"):
                first.setdefault(x["node"], {"node": x["node"], "count_keywords": 0, "count_also": 0})
                first[x["node"]]["count_keywords"] += 1
                first[x["node"]].setdefault("first_keywords", x["timestamp"])
        for x in (y for y in lines if _matches(y, also) and y.get("node") in first):
            first[x["node"]]["count_also"] += 1
            first[x["node"]].setdefault("first_also", x["timestamp"])
        rows = [{"match": f"{' '.join(terms)} + {' '.join(also)}", **r} for r in first.values() if r["count_also"]]
        rows.sort(key=lambda r: -(r["count_keywords"] + r["count_also"]))
    shown = _cap(rows)
    return ToolResult(tool="analyze_logs", status=ToolStatus.OK, source=f"test_data/logs/{incident_id}", data={
        "operation": operation, "keywords": " ".join(terms),
        **({"also_keywords": " ".join(also)} if operation in ("near", "nodes_with_both") else {}),
        **({"window_seconds": int(window.total_seconds())} if operation in ("near", "timeline") else {}),
        "total_rows": len(rows), "shown": len(shown), "rows": shown,
    })


# Words that mark a warning or error line in syslog (kernel, drivers, filesystem clients) and job manager logs.
# Fixed and generic, written before the digest was measured on any scenario; never extended to fit one fault.
DIGEST_TERMS = ("error", "fail", "timeout", "timed out", "down", "lost", "xid", "oom", "out of memory", "killed",
                "panic", "ecc", "segfault", "abort", "fatal", "critical", "reset", "unreachable", "evict",
                "throttl", "denied", "no space", "i/o", "hung", "not responding")
DIGEST_AFTER = timedelta(minutes=5)  # effects logged just after the job ended
DIGEST_DEFAULT_SPAN = timedelta(hours=2)  # before the job's end, when its start time is unknown
MAX_DIGEST_ROWS = 20
_DURATION = re.compile(r"(?:(\d+)-)?(\d+):(\d{2}):(\d{2})")
_VARIABLE = re.compile(r"0x[0-9a-f]+|\d+")


def _duration(value: object) -> timedelta | None:
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _ends(rows: list[dict]) -> list[dict]:
    """The earliest and the latest messages, alternately, within the row and size limits, kept in time order:
    the first error on a node and the last ones before the job ended both matter; the middle goes first."""
    keep, size, lo, hi, from_start = set(), 0, 0, len(rows) - 1, True
    while lo <= hi and len(keep) < MAX_DIGEST_ROWS:
        i = lo if from_start else hi
        size += len(json.dumps(rows[i]))
        if size > MAX_SHOWN_CHARS:
            break
        keep.add(i)
        lo, hi = (lo + 1, hi) if from_start else (lo, hi - 1)
        from_start = not from_start
    return [r for i, r in enumerate(rows) if i in keep]


def digest(adapter: ReadOnlyDataAdapter, incident_id: str) -> ToolResult:
    """Warning and error lines on the job's allocated nodes from its start to just after its end.

    What an engineer greps first: the job's own nodes, the job's own time, error words. Repeats of the
    same message (numbers masked) are folded into their first line with a count, oldest first, so the
    first error on a node comes before its consequences. When there are too many, the earliest and the latest
    are kept and the middle is omitted (counted). Lines elsewhere in the same window are only
    counted: they are the background an engineer has to rule out.
    """
    job_path = ("test_data", "scheduler", incident_id, "job_status.json")
    if not adapter.exists(*job_path):
        return ToolResult(tool="get_log_digest", status=ToolStatus.NO_DATA)
    job = adapter.read_json(*job_path)
    nodes = [n for n in job.get("allocated_nodes") or [] if isinstance(n, str)]
    end = _when({"timestamp": job.get("end_time")})
    if end is None or not nodes:
        return ToolResult(tool="get_log_digest", status=ToolStatus.NO_DATA)
    elapsed = _duration(job.get("elapsed"))
    start = end - (elapsed if elapsed is not None else DIGEST_DEFAULT_SPAN)
    job_id = job.get("job_id") if isinstance(job.get("job_id"), str) else None
    wanted = set(nodes)

    def on_job(line: dict) -> bool:
        if line.get("node"):
            return line["node"] in wanted
        text = line.get("text", "")  # job manager lines carry no node: keep those that name the job or its nodes
        return bool(job_id and job_id in text) or any(n in text for n in nodes)

    folded: dict[tuple[str, str], dict] = {}
    elsewhere, elsewhere_nodes, total = 0, set(), 0
    for line in _all_lines(adapter, incident_id):
        when = _when(line)
        text = line.get("text", "")
        if not (start <= when <= end + DIGEST_AFTER) or not any(t in text.lower() for t in DIGEST_TERMS):
            continue
        if not on_job(line):
            elsewhere += 1
            elsewhere_nodes.add(line.get("node"))
            continue
        total += 1
        key = (line.get("node") or line["log"], _VARIABLE.sub("#", text.lower()))
        if key in folded:
            folded[key]["repeats"] += 1
            folded[key]["last_seen"] = line["timestamp"]
        else:
            folded[key] = {**_where(line), "timestamp": line["timestamp"], "text": text, "repeats": 1,
                           "last_seen": line["timestamp"]}
    rows = _ends(list(folded.values()))
    return ToolResult(tool="get_log_digest", status=ToolStatus.OK, source=f"test_data/logs/{incident_id}", data={
        "job_id": job_id, "nodes": nodes, "window_start": start.isoformat(),
        "window_end": (end + DIGEST_AFTER).isoformat(), "total_matches": total, "messages": len(folded),
        "shown": len(rows), "omitted": len(folded) - len(rows), "rows": rows,
        "elsewhere": {"lines": elsewhere, "nodes": len(elsewhere_nodes - {None})},
    })


def tool_specs(adapter: ReadOnlyDataAdapter) -> list[ToolSpec]:
    return [ToolSpec(
        name="analyze_logs",
        description=("Analyse this incident's saved logs (all sources): count_by_node, timeline (counts per "
                     "window), near (lines matching also_keywords within window seconds after a keywords line), "
                     "nodes_with_both (nodes with lines matching both)"),
        operation=Operation.READ_TEST_DATA,
        parameters=frozenset({"incident_id", "operation", "keywords", "also_keywords", "window_seconds"}),
        handler=lambda incident_id, operation, keywords, also_keywords, window_seconds: analyze(
            adapter, incident_id, operation, keywords, also_keywords, window_seconds),
    ), ToolSpec(
        name="get_log_digest",
        description=("Warning and error lines on the failed job's own nodes from its start to 5 minutes after its "
                     "end (fixed error terms, repeats folded, oldest first), and how many such lines other nodes "
                     "logged in the same window"),
        operation=Operation.READ_TEST_DATA,
        parameters=frozenset({"incident_id"}),
        handler=lambda incident_id: digest(adapter, incident_id),
    ), ToolSpec(
        name="get_log_lines",
        description="Saved log lines of this incident that contain all the keywords (source: slurmctld, slurmd, pjm, syslog)",
        operation=Operation.READ_TEST_DATA,
        parameters=frozenset({"incident_id", "source", "keywords"}),
        handler=lambda incident_id, source, keywords: search(adapter, incident_id, source, keywords),
    )]
