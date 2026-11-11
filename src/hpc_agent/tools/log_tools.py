"""Log tools over the incident's saved, de-identified log index (read-only, saved data only).

get_log_lines: search one log source for lines containing all keywords.
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
        name="get_log_lines",
        description="Saved log lines of this incident that contain all the keywords (source: slurmctld, slurmd, pjm, syslog)",
        operation=Operation.READ_TEST_DATA,
        parameters=frozenset({"incident_id", "source", "keywords"}),
        handler=lambda incident_id, source, keywords: search(adapter, incident_id, source, keywords),
    )]
