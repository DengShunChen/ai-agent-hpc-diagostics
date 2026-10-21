"""Command-line entry point (offline; synthetic data only).

    python -m hpc_agent.main                     validate fixtures, list allowlisted tools
    python -m hpc_agent.main diagnose INC-ID     run triage + specialist diagnosis
"""

from __future__ import annotations

import argparse
import sys

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.tools.gateway import build_default_gateway


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hpc-agent")
    sub = parser.add_subparsers(dest="command")
    diagnose = sub.add_parser("diagnose", help="diagnose a fixture incident")
    diagnose.add_argument("incident_id")
    args = parser.parse_args(argv)

    adapter = ReadOnlyDataAdapter()
    gateway = build_default_gateway(adapter)
    if args.command == "diagnose":
        from hpc_agent.workflows.diagnostic_workflow import render_report, run_diagnosis

        print(render_report(run_diagnosis(adapter.load_incident(args.incident_id), gateway)))
        return 0

    for incident_id in adapter.list_incident_ids():
        incident = adapter.load_incident(incident_id)
        print(f"fixture OK: {incident.incident_id} severity={incident.severity}")
    print("registered tools:", ", ".join(gateway.registered_tools()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
