"""Adapter for raw exports: log files and saved command output, one directory per incident.

    <export>/<incident_id>/incident.json             the ticket: incident fields (see models/incident.py)
    <export>/<incident_id>/logs/slurmctld.log        "[YYYY-MM-DDTHH:MM:SS(.mmm)] message"
    <export>/<incident_id>/logs/slurmd/<node>.log    same format, one file per node
    <export>/<incident_id>/logs/pjm.log              "[YYYY-MM-DDTHH:MM:SS(.mmm)] message" (PJM job manager)
    <export>/<incident_id>/logs/syslog               "<RFC 3339 time> <host> <message>" (central syslog)
    <export>/<incident_id>/commands/sacct.txt        `sacct -P` with JobID|JobName|Partition|State|ExitCode|
                                                     Elapsed|Timelimit|Start|End|NodeList
    <export>/<incident_id>/commands/sinfo.txt        `sinfo -N -o "%N|%T|%E|%H"` with a header row
    <export>/<incident_id>/commands/lfs_df.txt       `lfs df` output (optional "# collected <time>" line)
    <export>/<incident_id>/commands/ibstat.txt       per node: "## <node>", ibstat port block, perfquery counters
    <export>/<incident_id>/commands/dcgmi_health.txt per node: "## <node>", "GPU i | PCI x | health | temp N C | DBE n |"
    <export>/<incident_id>/commands/pjstat.txt       PJM job history as CSV: JOB_ID,JOB_NAME,MD,ST,START_DATE,END_DATE,
                                                     ELAPSE_TIM,ELAPSE_LIM,NODE,NODE_LIST,PJM_CODE,END_CODE,SIGNAL,REASON
    <export>/<incident_id>/commands/node_state.txt   NODELIST|STATE|REASON|TIMESTAMP (same layout as sinfo.txt)
    <export>/<incident_id>/commands/tofu_links.txt   per node: "## <node>", "TNIn port X: state=up|down link_down_count=N
                                                     crc_errors=N"

The PJM and Tofu layouts follow what the simulator writes (simulate/platform.py); they are an
assumption to check against a real site export before use, not a documented product format.

Every file is optional except incident.json. Parsing is deterministic and only reads files: a
value that can't be parsed stays missing, a log line without a recognizable prefix is counted and
left out, and nothing is guessed. Lines starting with "#" are comments ("## " starts a node section). Log text is kept verbatim;
the importer then pseudonymizes node, job and filesystem names in it and scans it like any field.

Limitation: a host name that appears only inside log text (never as a syslog host, a slurmd file
name, or in a command output) is not known to the pseudonymizer and stays as written.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hpc_agent.ingest.importer import ExportBundle, ExportImportError
from hpc_agent.models.incident import INCIDENT_ID_PATTERN

MAX_FILE_BYTES = 5_000_000
MAX_LINES_PER_SOURCE = 6000
NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRACKET = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)\] (.+)$")
_SYSLOG = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})) (\S+) (.+)$")
_HOSTLIST = re.compile(r"^(?P<prefix>[A-Za-z0-9._-]+)\[(?P<ranges>[0-9,-]+)\]$")  # prefix kept as written
_DCGM = re.compile(r"^GPU (\d+) \| PCI (\S+) \| (\w+) \| temp (-?\d+) C \| DBE (\d+) \|")
_XID = re.compile(r"NVRM: Xid \(PCI:([0-9a-fA-F:.]+)\): (\d+),")
_COUNTERS = {"SymbolErrorCounter": "symbol", "LinkDownedCounter": "link_downed", "PortRcvErrors": "rcv",
             "PortXmitDiscards": "xmit"}


class RawExportAdapter:
    name = "raw-export"

    def __init__(self, directory: Path) -> None:
        if not directory.is_dir():
            raise ExportImportError("export directory not found")
        self._dir = directory

    def incident_ids(self) -> list[str]:
        return sorted(p.name for p in self._dir.iterdir()
                      if p.is_dir() and re.match(INCIDENT_ID_PATTERN, p.name) and (p / "incident.json").is_file())

    def load(self, incident_id: str) -> ExportBundle:
        base = self._dir / incident_id
        incident = json.loads(_read(base / "incident.json"))
        datasets: dict[tuple[str, str], dict[str, Any]] = {}
        logs = base / "logs"
        if (logs / "slurmctld.log").is_file():
            datasets[("logs", "slurmctld")] = _bracket_log(_lines(logs / "slurmctld.log"), "slurmctld", None)
        if (logs / "pjm.log").is_file():
            datasets[("logs", "pjm")] = _bracket_log(_lines(logs / "pjm.log"), "pjm", None)
        slurmd_files = sorted((logs / "slurmd").glob("*.log")) if (logs / "slurmd").is_dir() else []
        if slurmd_files:
            entries, unparsed = [], 0
            for path in slurmd_files:
                node = path.stem
                if not NODE_NAME.match(node):
                    raise ValueError("slurmd log file name is not a node name")
                part = _bracket_log(_lines(path), "slurmd", node)
                entries += part["lines"]
                unparsed += part["unparsed"]
            datasets[("logs", "slurmd")] = {"source": "slurmd", "lines": _cap(entries), "unparsed": unparsed}
        if (logs / "syslog").is_file():
            datasets[("logs", "syslog")] = _syslog(_lines(logs / "syslog"))

        commands = base / "commands"
        job_id = incident.get("job_id")
        if (commands / "sacct.txt").is_file() and isinstance(job_id, str):
            job = _sacct(_lines(commands / "sacct.txt"), job_id)
            if job:
                datasets[("scheduler", "job_status")] = job
        if (commands / "pjstat.txt").is_file() and isinstance(job_id, str):
            job = _pjstat(_lines(commands / "pjstat.txt"), job_id)
            if job:
                datasets[("scheduler", "job_status")] = job
        for name in ("sinfo.txt", "node_state.txt"):
            if (commands / name).is_file():
                nodes = _sinfo(_lines(commands / name))
                if nodes:
                    datasets[("scheduler", "node_status")] = {"nodes": nodes}
        if (commands / "lfs_df.txt").is_file():
            datasets.update(_lfs_df(_lines(commands / "lfs_df.txt")))
        if (commands / "ibstat.txt").is_file():
            interfaces = _ibstat(_lines(commands / "ibstat.txt"))
            if interfaces:
                datasets[("network", "network_status")] = {"interfaces": interfaces}
        if (commands / "tofu_links.txt").is_file():
            interfaces = _tofu(_lines(commands / "tofu_links.txt"))
            if interfaces:
                datasets[("network", "network_status")] = {"interfaces": interfaces}
        if (commands / "dcgmi_health.txt").is_file():
            gpu = _dcgmi(_lines(commands / "dcgmi_health.txt"), datasets.get(("logs", "syslog")))
            if gpu:
                datasets[("gpu", "gpu_health")] = gpu
        return ExportBundle(incident=incident, datasets=datasets)


# --- reading ---------------------------------------------------------------------------------------

def _read(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name}: not a regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path.name}: larger than {MAX_FILE_BYTES} bytes")
    return path.read_text(encoding="utf-8", errors="replace")


def _lines(path: Path) -> list[tuple[int, str]]:
    """(physical line number, text) for every non-empty, non-comment line."""
    return [(n, line.rstrip("\n")) for n, line in enumerate(_read(path).splitlines(), start=1)
            if line.strip() and (not line.startswith("#") or line.startswith("## "))]


def _time(text: str) -> str | None:
    """An ISO time with a timezone (UTC assumed for Slurm's local-time stamps), or None."""
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _cap(entries: list[dict]) -> list[dict]:
    if len(entries) > MAX_LINES_PER_SOURCE:
        raise ValueError(f"more than {MAX_LINES_PER_SOURCE} log lines in one source; export a narrower window")
    return entries


def _bracket_log(lines: list[tuple[int, str]], source: str, node: str | None) -> dict:
    """node is None for the job manager's log: its lines carry no host (see LogLine)."""
    entries, unparsed = [], 0
    for number, line in lines:
        match = _BRACKET.match(line)
        when = _time(match.group(1)) if match else None
        if not match or not when:
            unparsed += 1
            continue
        entry = {"line": number, "timestamp": when, "text": match.group(2)[:1000]}
        entries.append({"node": node, **entry} if node else entry)
    return {"source": source, "lines": _cap(entries), "unparsed": unparsed}


def _syslog(lines: list[tuple[int, str]]) -> dict:
    entries, unparsed = [], 0
    for number, line in lines:
        match = _SYSLOG.match(line)
        when = _time(match.group(1)) if match else None
        if not match or not when or not NODE_NAME.match(match.group(2)):
            unparsed += 1
            continue
        entries.append({"line": number, "node": match.group(2), "timestamp": when, "text": match.group(3)[:1000]})
    return {"source": "syslog", "lines": _cap(entries), "unparsed": unparsed}


# --- command output ----------------------------------------------------------------------------------

def expand_hostlist(text: str) -> list[str]:
    """sim-node-[0003-0005,0009] -> [sim-node-0003, ...]; node[01-02] -> [node01, node02] (the prefix is kept
    exactly as written, as in Slurm and PJM node lists); comma-separated plain names are split."""
    names: list[str] = []
    for part in re.split(r",(?![^\[]*\])", text.strip()):
        match = _HOSTLIST.match(part)
        if not match:
            if NODE_NAME.match(part):
                names.append(part)
            continue
        prefix = match.group("prefix")
        for item in match.group("ranges").split(","):
            low, _, high = item.partition("-")
            width = len(low)
            for n in range(int(low), int(high or low) + 1):
                names.append(f"{prefix}{n:0{width}d}")
    return names


def _exit_code(text: str) -> int | None:
    """sacct ExitCode "exit:signal": a real signal (1-64) becomes 128+signal, otherwise the exit status."""
    code, _, sig = text.partition(":")
    if not code.isdigit() or not sig.isdigit():
        return None
    if int(sig) == 0:
        return int(code)
    return 128 + int(sig) if 1 <= int(sig) <= 64 else None


def _sacct(lines: list[tuple[int, str]], job_id: str) -> dict | None:
    if not lines:
        return None
    header = lines[0][1].split("|")
    for _, line in lines[1:]:
        row = dict(zip(header, line.split("|"), strict=False))
        if row.get("JobID") != job_id:
            continue
        end = _time(row.get("End", ""))
        return {k: v for k, v in {
            "job_id": job_id, "scheduler": "slurm", "state": row.get("State") or None,
            "exit_code": _exit_code(row.get("ExitCode", "")),
            "requested_walltime": row.get("Timelimit") or None, "elapsed": row.get("Elapsed") or None,
            "end_time": end, "allocated_nodes": expand_hostlist(row.get("NodeList", "")),
        }.items() if v not in (None, [])}
    return None


def _pjstat(lines: list[tuple[int, str]], job_id: str) -> dict | None:
    if not lines:
        return None
    rows = list(csv.reader(text for _, text in lines))  # CSV: a quoted NODE_LIST may itself contain commas
    header = rows[0]
    for values in rows[1:]:
        row = dict(zip(header, values, strict=False))
        if row.get("JOB_ID") != job_id:
            continue
        code, sig = row.get("END_CODE", ""), row.get("SIGNAL", "")
        reason = row.get("REASON")
        return {k: v for k, v in {
            "job_id": job_id, "scheduler": "pjm", "state": row.get("ST") or None,
            "exit_code": _exit_code(f"{code}:{sig}"),
            "requested_walltime": row.get("ELAPSE_LIM") or None, "elapsed": row.get("ELAPSE_TIM") or None,
            "end_time": _time(row.get("END_DATE", "")), "allocated_nodes": expand_hostlist(row.get("NODE_LIST", "")),
            "termination_reason": reason if reason and reason != "-" else None,
        }.items() if v not in (None, [])}
    return None


_TOFU = re.compile(r"^(TNI\d+) port ([A-F]): state=(\w+) link_down_count=(\d+) crc_errors=(\d+)$")


def _tofu(lines: list[tuple[int, str]]) -> list[dict]:
    interfaces, node = [], None
    for _, line in lines:
        if line.startswith("## "):
            node = line[3:].strip()
            continue
        match = _TOFU.match(line.strip())
        if match and node:
            interfaces.append({"node": node, "interface": f"tofu/{match.group(1)}-{match.group(2)}",
                               "state": match.group(3).lower(), "link_flaps": int(match.group(4)),
                               "rx_errors": int(match.group(5))})
    return interfaces


def _sinfo(lines: list[tuple[int, str]]) -> list[dict]:
    if not lines:
        return []
    header = lines[0][1].split("|")
    nodes = []
    for _, line in lines[1:]:
        row = dict(zip(header, line.split("|"), strict=False))
        state = (row.get("STATE") or "").rstrip("*~#!%$@^-").upper() or None
        reason = row.get("REASON")
        for name in expand_hostlist(row.get("NODELIST", "")):
            entry = {"node": name, "state": state}
            if reason and reason.lower() != "none":
                entry["reason"] = reason
            when = _time(row.get("TIMESTAMP", ""))
            if when and entry.get("reason"):
                entry["state_changed_at"] = when
            nodes.append(entry)
    return nodes


def _lfs_df(lines: list[tuple[int, str]]) -> dict[tuple[str, str], dict]:
    osts, summary = [], None
    for _, line in lines:
        fields = line.split()
        if len(fields) >= 6 and fields[0].endswith("_UUID") and "-OST" in fields[0] and fields[4].endswith("%"):
            osts.append({"ost": fields[0].rsplit("-", 1)[1].removesuffix("_UUID"),
                         "used_pct": float(fields[4].rstrip("%"))})
        elif fields and fields[0] == "filesystem_summary:" and len(fields) >= 6 and fields[4].endswith("%"):
            summary = {"filesystem": fields[5].lstrip("/"), "used_pct": float(fields[4].rstrip("%"))}
    out: dict[tuple[str, str], dict] = {}
    if osts:
        out[("storage", "ost_usage")] = {"osts": osts}
    if summary:
        out[("storage", "filesystem_usage")] = summary
    return out


def _ibstat(lines: list[tuple[int, str]]) -> list[dict]:
    interfaces: list[dict] = []
    node = ca = None
    current: dict | None = None
    counters: dict[str, int] = {}

    def flush() -> None:
        if current is not None:
            current.update({"rx_errors": counters.get("rcv", 0) + counters.get("symbol", 0),
                            "tx_errors": counters.get("xmit", 0), "link_flaps": counters.get("link_downed", 0)}
                           if counters else {})
            interfaces.append(current)

    for _, line in lines:
        text = line.strip()
        if line.startswith("## ") and not line.startswith("## perfquery"):
            flush()
            node, ca, current, counters = line[3:].strip(), None, None, {}
        elif text.startswith("CA '"):
            ca = text[4:].rstrip("'")
        elif text.startswith("Port ") and node and ca:
            current = {"node": node, "interface": f"{ca}/{text[5:].rstrip(':')}"}
        elif text.startswith("State:") and current is not None:
            current["state"] = "up" if text.split(":", 1)[1].strip().lower() == "active" else "down"
        elif ":" in text and text.split(":", 1)[0] in _COUNTERS:
            value = text.rsplit(".", 1)[-1].lstrip(":").strip()
            if value.isdigit():
                counters[_COUNTERS[text.split(":", 1)[0]]] = int(value)
    flush()
    return interfaces


def _dcgmi(lines: list[tuple[int, str]], syslog: dict | None) -> dict | None:
    """The first node's GPUs (the gpu_health dataset describes one node); XID events from syslog."""
    node, gpus, pci_index = None, [], {}
    for _, line in lines:
        if line.startswith("## "):
            if node is not None:
                break
            node = line[3:].strip()
            continue
        match = _DCGM.match(line.strip())
        if match and node:
            index, pci = int(match.group(1)), match.group(2).lower()
            pci_index[pci.rsplit(".", 1)[0]] = index
            gpus.append({"index": index, "health": match.group(3).lower(), "temperature_c": float(match.group(4)),
                         "ecc_dbe_volatile": int(match.group(5)), "xid_events": []})
    if not node or not gpus:
        return None
    by_index = {g["index"]: g for g in gpus}
    for entry in (syslog or {}).get("lines", []):
        match = _XID.search(entry["text"]) if entry["node"] == node else None
        index = pci_index.get(match.group(1).lower()) if match else None
        if index is not None:
            events = by_index[index]["xid_events"]
            xid = int(match.group(2))
            same = next((e for e in events if e["xid"] == xid), None)
            if same:
                same["count"] += 1
            else:
                events.append({"xid": xid, "count": 1, "timestamp": entry["timestamp"]})
    return {"node": node, "format": "dcgmi-style health summary with XID events from syslog", "gpus": gpus}
