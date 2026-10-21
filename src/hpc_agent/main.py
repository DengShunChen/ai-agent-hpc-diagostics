"""Iteration 1 entry point: validate synthetic fixtures and show the tool allowlist.

No agents, LLM calls or external connections exist yet.
"""

from __future__ import annotations

import sys

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.tools.gateway import build_default_gateway


def main() -> int:
    adapter = ReadOnlyDataAdapter()
    for incident_id in adapter.list_incident_ids():
        incident = adapter.load_incident(incident_id)
        print(f"fixture OK: {incident.incident_id} severity={incident.severity}")
    print("registered tools:", ", ".join(build_default_gateway(adapter).registered_tools()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
