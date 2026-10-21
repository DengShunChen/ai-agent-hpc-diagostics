"""Command-line entry point: the human operator's interface (offline; synthetic data only).

    hpc-agent                                         validate fixtures, list allowlisted tools
    hpc-agent diagnose INC-ID [--no-save]             diagnose; saved runs await human approval
    hpc-agent review RUN-ID                           show the approval package
    hpc-agent approve RUN-ID --operator NAME [--comment TEXT]
    hpc-agent reject RUN-ID --operator NAME --comment TEXT
    hpc-agent request-revision RUN-ID --operator NAME --comment TEXT
    hpc-agent revise RUN-ID --operator NAME           re-diagnose a rejected run as a new run
    hpc-agent close RUN-ID --operator NAME
    hpc-agent history RUN-ID                          approval transitions
    hpc-agent verify-audit                            verify both hash chains

Decisions are recorded only; nothing is ever executed. --operator is an asserted identity
(no authentication in this POC).
"""

from __future__ import annotations

import argparse
import sys

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.models import ApprovalStatus
from hpc_agent.tools.gateway import build_default_gateway

DECISIONS = {
    "approve": ApprovalStatus.APPROVED,
    "reject": ApprovalStatus.REJECTED,
    "request-revision": ApprovalStatus.REVISION_REQUESTED,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hpc-agent")
    sub = parser.add_subparsers(dest="command")
    diagnose = sub.add_parser("diagnose", help="diagnose a fixture incident")
    diagnose.add_argument("incident_id")
    diagnose.add_argument("--no-save", action="store_true", help="do not record the run for approval")
    for name in ("review", "history"):
        sub.add_parser(name).add_argument("run_id")
    for name in [*DECISIONS, "revise", "close"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("run_id")
        cmd.add_argument("--operator", required=True, help="human operator identity")
        cmd.add_argument("--comment", default="")
    sub.add_parser("verify-audit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    adapter = ReadOnlyDataAdapter()
    gateway = build_default_gateway(adapter)

    if args.command is None:
        for incident_id in adapter.list_incident_ids():
            incident = adapter.load_incident(incident_id)
            print(f"fixture OK: {incident.incident_id} severity={incident.severity}")
        print("registered tools:", ", ".join(gateway.registered_tools()))
        return 0

    from hpc_agent.workflows.approval_workflow import ApprovalService, default_state_dir, render_review
    from hpc_agent.workflows.diagnostic_workflow import render_report, run_diagnosis

    service = ApprovalService(default_state_dir(), adapter, gateway)
    try:
        if args.command == "diagnose":
            incident = adapter.load_incident(args.incident_id)
            run = run_diagnosis(incident, gateway) if args.no_save else service.diagnose(incident)
            print(render_report(run))
            if not args.no_save:
                print(f"\nRun {run.run_id} recorded: {service.status(run.run_id).status}")
                print(f"Review with: hpc-agent review {run.run_id}")
        elif args.command == "review":
            print(render_review(service.review(args.run_id)))
        elif args.command in DECISIONS:
            record = service.decide(args.run_id, DECISIONS[args.command], args.operator, args.comment)
            print(f"{record.run_id}: {record.status} by {record.operator} at {record.timestamp.isoformat()}")
            print("Recorded only. Nothing has been executed.")
        elif args.command == "close":
            record = service.close(args.run_id, args.operator, args.comment)
            print(f"{record.run_id}: {record.status} by {record.operator}")
        elif args.command == "revise":
            run = service.revise(args.run_id, args.operator, args.comment)
            print(f"{args.run_id}: REVISE. New run {run.run_id} awaits approval (supersedes {run.supersedes}).")
        elif args.command == "history":
            for r in service.history(args.run_id):
                who = f" by {r.operator}" if r.operator else ""
                note = f" - {r.operator_comment}" if r.operator_comment else ""
                print(f"{r.timestamp.isoformat()}  {r.status}{who}{note}")
        elif args.command == "verify-audit":
            print(f"audit.jsonl: {service.audit.verify()} records, chain OK")
            print(f"approvals.jsonl: {service.approvals.verify()} records, chain OK")
    except (KeyError, ValueError, PermissionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
