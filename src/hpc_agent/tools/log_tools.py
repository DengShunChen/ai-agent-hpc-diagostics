"""get_log_lines: search one saved log source of an incident (read-only, saved data only).

The logs are the de-identified index written by the importer (test_data/logs/<id>/<source>.json).
A line matches when it contains every keyword (case-insensitive) in its node name or text: plain
substring search, no regular expressions. Results are capped so the model always sees whole lines,
and the total count tells it to narrow the search.
"""

from __future__ import annotations

import json

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


def tool_specs(adapter: ReadOnlyDataAdapter) -> list[ToolSpec]:
    return [ToolSpec(
        name="get_log_lines",
        description="Saved log lines of this incident that contain all the keywords (source: slurmctld, slurmd, pjm, syslog)",
        operation=Operation.READ_TEST_DATA,
        parameters=frozenset({"incident_id", "source", "keywords"}),
        handler=lambda incident_id, source, keywords: search(adapter, incident_id, source, keywords),
    )]
