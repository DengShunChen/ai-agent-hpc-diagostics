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
    hpc-agent serve [--host 127.0.0.1] [--port 8080]  HTTP API + review page
    hpc-agent create-operator-token NAME --tokens-file PATH   prints a new token once
    hpc-agent evaluate [--output docs/evaluation.md] [--approvals-log PATH]   score labelled cases
    hpc-agent explain RUN-ID [--operator NAME]        local-LLM explanation of a run (MODEL_PROVIDER=ollama)
    hpc-agent metrics [--json]                        workflow timings per site (not MTTR)
    hpc-agent compare-pilot FILE.csv [--min-n 10]     baseline vs assisted localization time / MTTR
    hpc-agent import-export DIR --output DATA_ROOT [--dry-run]   validate + de-identify a site's CSV export

Decisions are recorded only; nothing is ever executed. On the CLI, --operator is an asserted
identity; the HTTP API instead derives the operator from an authenticated bearer token.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.models import ApprovalStatus
from hpc_agent.tools.gateway import build_default_gateway

DECISIONS = {
    "approve": ApprovalStatus.APPROVED,
    "reject": ApprovalStatus.REJECTED,
    "request-revision": ApprovalStatus.REVISION_REQUESTED,
}


def _cloud_problem(provider: object, adapter: ReadOnlyDataAdapter) -> str | None:
    """Claude is allowed only on synthetic or de-identified imported data (fixtures/DATA_CLASS.json)."""
    if getattr(provider, "name", None) != "anthropic":
        return None
    from hpc_agent.llm.claude import cloud_data_problem

    return cloud_data_problem(adapter.data_class())


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
    sub.add_parser("metrics", help="workflow timings and override rate per site").add_argument(
        "--json", action="store_true")
    pilot = sub.add_parser("compare-pilot", help="compare baseline vs assisted pilot periods")
    pilot.add_argument("csv", type=Path)
    pilot.add_argument("--min-n", type=int, default=10)
    imp = sub.add_parser("import-export", help="import a site's exported CSV files into a separate data root")
    imp.add_argument("export_dir", type=Path)
    imp.add_argument("--output", type=Path, required=True, help="new data root, outside the repository")
    imp.add_argument("--dry-run", action="store_true", help="validate and report only; write nothing")
    explain = sub.add_parser("explain", help="ask the local LLM to explain a run's evidence")
    explain.add_argument("run_id")
    explain.add_argument("--operator", default=None)
    serve = sub.add_parser("serve", help="run the HTTP API and review page")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    token = sub.add_parser("create-operator-token", help="create or rotate an operator's API token")
    token.add_argument("operator")
    token.add_argument("--tokens-file", required=True, type=Path)
    evaluate = sub.add_parser("evaluate", help="run the labelled evaluation cases")
    evaluate.add_argument("--cases", type=Path, default=None, help="default: evaluation/cases")
    evaluate.add_argument("--approvals-log", type=Path, default=None,
                          help="approvals.jsonl with real human decisions (for the override rate)")
    evaluate.add_argument("--output", type=Path, default=None, help="write the Markdown report here")
    evaluate.add_argument("--json", type=Path, default=None, help="write per-case results as JSON here")
    evaluate.add_argument("--reasoning", choices=("rules", "llm"), default="rules",
                          help="llm: diagnose with the LLM-driven agents (needs MODEL_PROVIDER; slow on CPU)")
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

    from hpc_agent.logging.audit_logger import AuditIntegrityError
    from hpc_agent.safety.operators import OperatorAuthError, OperatorTokens, issue_token
    from hpc_agent.workflows.approval_workflow import ApprovalService, default_state_dir, render_review
    from hpc_agent.workflows.diagnostic_workflow import render_report, run_diagnosis

    if args.command == "compare-pilot":
        from hpc_agent.evaluation import pilot as pilot_analysis

        if args.min_n < 5:
            print("error: --min-n below 5 is not allowed", file=sys.stderr)
            return 2
        incidents, problems = pilot_analysis.load(args.csv)
        print(pilot_analysis.render(pilot_analysis.analyse(incidents, args.min_n), problems))
        return 0

    if args.command == "import-export":
        from hpc_agent.ingest.csv_export import CsvExportAdapter
        from hpc_agent.ingest.deidentify import DeidentificationError, Pseudonymizer
        from hpc_agent.ingest.importer import ExportImportError, import_export

        try:
            report = import_export(CsvExportAdapter(args.export_dir), args.output, Pseudonymizer.from_env(),
                                   forbidden_root=adapter.root, dry_run=args.dry_run)
        except (ExportImportError, DeidentificationError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(report.render())
        if report.imported and not args.dry_run:
            print(f"next: put the site's knowledge/ into {args.output}, then start the container with "
                  f"HPC_AGENT_DATA_DIR={args.output} (see docs/pilot-runbook.md)")
        return 1 if report.rejected else 0

    if args.command == "evaluate":
        import json

        from hpc_agent.evaluation.harness import evaluate as run_evaluation
        from hpc_agent.evaluation.report import render_markdown

        from hpc_agent.llm.provider import LLMConfigError, provider_from_env

        llm = None
        if args.reasoning == "llm":
            try:
                llm = provider_from_env()
            except LLMConfigError as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
            if llm is None:
                print("error: --reasoning llm needs MODEL_PROVIDER=ollama and MODEL_NAME", file=sys.stderr)
                return 2
        results, summary = run_evaluation(args.cases or adapter.root / "evaluation" / "cases",
                                          approvals_log=args.approvals_log, llm=llm)
        report = render_markdown(results, summary)
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        if args.json:
            args.json.write_text(json.dumps({"summary": summary.model_dump(),
                                             "cases": [r.model_dump() for r in results]}, indent=2))
        print(report)
        return 0

    if args.command == "create-operator-token":
        try:
            secret = issue_token(args.tokens_file, args.operator, forbidden_root=adapter.root)
        except (OperatorAuthError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(f"Token for {args.operator} (shown once; only its hash is stored in {args.tokens_file}):")
        print(secret)
        return 0

    from hpc_agent.llm.provider import LLMConfigError, provider_from_env, reasoning_llm_from_env

    try:
        reasoning_llm = reasoning_llm_from_env()
        explainer_llm = provider_from_env()
    except LLMConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    problem = _cloud_problem(reasoning_llm, adapter) or _cloud_problem(explainer_llm, adapter)
    if problem and args.command in ("serve", "diagnose", "explain"):
        print(f"error: {problem}", file=sys.stderr)
        return 2
    service = ApprovalService(default_state_dir(), adapter, gateway, reasoning_llm=reasoning_llm)

    if args.command == "serve":
        import uvicorn

        from hpc_agent.api.routes import create_app

        tokens_path = os.environ.get("HPC_AGENT_OPERATOR_TOKENS")
        tokens = None
        if tokens_path:
            tokens = OperatorTokens.from_file(Path(tokens_path), forbidden_root=adapter.root)
        else:
            print("warning: HPC_AGENT_OPERATOR_TOKENS not set; all authenticated endpoints will refuse",
                  file=sys.stderr)
        llm = explainer_llm
        print(f"LLM explainer: {f'{llm.name}:{llm.model}' if llm else 'not configured'}", file=sys.stderr)
        print(f"Agent reasoning: {f'LLM ({reasoning_llm.name}:{reasoning_llm.model}), checked by rules' if reasoning_llm else 'rules'}",
              file=sys.stderr)
        uvicorn.run(create_app(service, tokens, llm), host=args.host, port=args.port, server_header=False)
        return 0
    try:
        if args.command == "diagnose":
            incident = adapter.load_incident(args.incident_id)
            if reasoning_llm:
                print(f"Agents reasoning with {reasoning_llm.name}:{reasoning_llm.model} (local; may take minutes)...",
                      file=sys.stderr)
            run = (run_diagnosis(incident, gateway, llm=reasoning_llm) if args.no_save
                   else service.diagnose(incident))
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
        elif args.command == "explain":
            llm = explainer_llm
            if llm is None:
                print("error: no LLM configured; set MODEL_PROVIDER=ollama (and MODEL_NAME, MODEL_ENDPOINT)",
                      file=sys.stderr)
                return 2
            print(f"Asking {llm.name}:{llm.model} (local) to explain {args.run_id}...", file=sys.stderr)
            result = service.explain(args.run_id, llm, operator=args.operator)
            print(f"Status: {result.status}  ({result.duration_ms / 1000:.1f} s)")
            if result.text:
                print(f"\n{result.text}\n")
                print(f"[{result.disclaimer}]")
            for reason in result.rejection_reasons:
                print(f"  - {reason}")
        elif args.command == "metrics":
            from hpc_agent import metrics

            summary = metrics.summarize(metrics.collect(service))
            if args.json:
                import json

                print(json.dumps([m.model_dump() for m in summary], indent=2))
            else:
                print(metrics.render(summary))
        elif args.command == "verify-audit":
            print(f"audit.jsonl: {service.audit.verify()} records, chain OK")
            print(f"approvals.jsonl: {service.approvals.verify()} records, chain OK")
            print(f"explanations.jsonl: {service.explanations.verify()} records, chain OK")
    except (KeyError, ValueError, PermissionError, AuditIntegrityError, LLMConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
