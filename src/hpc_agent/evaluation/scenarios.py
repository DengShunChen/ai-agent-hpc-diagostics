"""Evaluate on simulated scenarios: raw logs and command outputs with an injected root cause.

Each scenario goes the way a site export would: the raw-export adapter parses it, the importer
validates, pseudonymizes and scans it into a temporary data root, and the normal workflow diagnoses
it. Labels are read from <dir>/labels/, which the adapter never sees. Scoring reuses harness.score;
on top of it, this records whether the agents cited the injected key log lines.

With a runs directory, every case's DiagnosticRun and agent trace are kept, and a manifest records
what was run (code, configuration, data and model). A case that fails (a timeout, an exhausted
budget, any error) is kept in the results and scored as wrong on every metric, never dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as host_platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from hpc_agent.data_adapter import ReadOnlyDataAdapter, default_data_root
from hpc_agent.evaluation.harness import Case, CaseResult, Expectation, Summary, score, summarize
from hpc_agent.ingest.deidentify import Pseudonymizer
from hpc_agent.ingest.importer import import_export
from hpc_agent.ingest.raw_export import RawExportAdapter
from hpc_agent.llm.provider import ChatLLM
from hpc_agent.simulate.catalog import EVIDENCE
from hpc_agent.simulate.scenario import Labels
from hpc_agent.runtime.trace import TraceRecorder
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis

# Simulated data only: a fixed, published key is fine because nothing real is pseudonymized with it.
SIMULATION_KEY = "simulated-scenarios-only-not-a-secret-0000"
_LOG_FACT = re.compile(r"(?:node=(?P<node>[^,]+), )?line=(?P<line>\d+):")
# Job manager logs: imported lines carry no node (see ingest/contract.py LogLine), so match on line only.
SCHEDULER_LOGS = ("slurmctld", "pjm")


class LogResult(BaseModel):
    case_id: str
    fault: str
    total_lines: int
    key_lines: int
    key_lines_cited: int
    log_searches: int
    log_analyses: int = 0  # analyze_logs calls
    consults: int = 0  # agents that ran because another agent consulted them
    evidence: str = ""  # where a novel fault's evidence is (simulate/catalog.py EVIDENCE); "" for known faults
    error: str = ""  # why the case failed (it is then scored as wrong); "" when it ran
    # Does the leading hypothesis name the labelled faulty component? None when the label names none.
    component_hit: bool | None = None
    fallbacks: list[str] = []  # agents that fell back to rules, with the reason


def _cited(run, pseudonymizer: Pseudonymizer) -> set[tuple[str, str, int]]:
    """(source, pseudonymized node, line) of every log line cited as evidence anywhere in the run."""
    out = set()
    evidence = [e for d in run.diagnoses for e in d.evidence] + list(run.rca.facts)
    for c in run.rca.probable_causes:
        evidence += c.supporting_evidence + c.conflicting_evidence
    for e in evidence:
        match = _LOG_FACT.search(e.statement)
        if match and e.source and "/logs/" in e.source:
            log = re.search(r"\blog=(\w+)", e.statement)  # analyze_logs rows name their source
            source = log.group(1) if log else e.source.rsplit("/", 1)[-1].removesuffix(".json")
            out.add((source, match.group("node"), int(match.group("line"))))
    return out


def _failed(case: Case, error: Exception) -> CaseResult:
    """A case that did not finish counts as wrong on every metric (it is never left out)."""
    exp = case.expected
    wrong = "no_supported_hypothesis" if exp.outcome != "no_supported_hypothesis" else "supported_hypothesis"
    return CaseResult(case_id=case.case_id, description=f"{case.description}; FAILED: {type(error).__name__}",
                      label_source=case.label_source, classification="error", classification_correct=False,
                      expected_outcome=exp.outcome, outcome=wrong, outcome_correct=False, leading_category=None,
                      leading_cause=None, leading_confidence=0.0,
                      leading_correct=False if exp.leading_categories else None,
                      top3_correct=False if exp.leading_categories else None, open_hypotheses=0,
                      tool_backed_hypotheses=0, unsupported_claims=[], missing_required=len(exp.required_missing),
                      missing_found=0, facts_reported=0, facts_tool=0, references=0, proposed_actions=0,
                      duration_ms=0.0)


def _digest(paths: list[Path], root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in paths if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _tree(root: Path, *parts: str) -> str:
    base = root.joinpath(*parts)
    files = [p for p in base.rglob("*") if "__pycache__" not in p.parts and p.suffix != ".pyc"]
    return _digest(files, root) if base.exists() else "absent"


def _git(repo: Path) -> dict:
    """Commit and tags read from .git files (no git command is run). The src hash pins the exact code."""
    git = repo / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        commit = (git / head[5:]).read_text().strip() if head.startswith("ref: ") and (git / head[5:]).exists() else head
        if head.startswith("ref: ") and not (git / head[5:]).exists():
            packed = (git / "packed-refs").read_text().splitlines() if (git / "packed-refs").exists() else []
            commit = next((ln.split()[0] for ln in packed if ln.endswith(" " + head[5:])), "unknown")
        tags = [p.name for p in (git / "refs" / "tags").glob("*") if p.read_text().strip() == commit]
        return {"commit": commit, "tags": sorted(tags)}
    except OSError:
        return {"commit": "unknown", "tags": []}


def manifest(directory: Path, llm: ChatLLM | None) -> dict:
    """What a run used, so another person can reproduce or audit it. No secrets: names and hashes only."""
    repo = default_data_root()
    from hpc_agent.runtime.skills import SkillLibrary

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "code": _git(repo),
        "hashes": {"src": _tree(repo, "src", "hpc_agent"), "config_agents": _tree(repo, "config", "agents"),
                   "config_sites": _tree(repo, "config", "sites"), "knowledge": _tree(repo, "knowledge"),
                   "scenario_export": _tree(directory, "export"), "scenario_labels": _tree(directory, "labels")},
        "skills_version": SkillLibrary.load(repo).version(),
        "model": None if llm is None else {"provider": llm.name, "model": llm.model,
                                           "timeout_s": os.environ.get("HPC_AGENT_LLM_TIMEOUT")},
        "reasoning": "rules" if llm is None else "llm",
        "host": {"python": sys.version.split()[0], "platform": host_platform.platform(), "cpus": os.cpu_count()},
    }


def evaluate_scenarios(
    directory: Path, scopes: Mapping[str, frozenset[str]] | None = None, llm: ChatLLM | None = None,
    runs_dir: Path | None = None,
) -> tuple[list[CaseResult], Summary, list[LogResult]]:
    labels = {p.stem: Labels.model_validate_json(p.read_text(encoding="utf-8"))
              for p in sorted((directory / "labels").glob("*.json"))}
    results, logs = [], []
    with tempfile.TemporaryDirectory(prefix="hpc-agent-scenarios-") as tmp:
        root = Path(tmp) / "root"
        shutil.copytree(default_data_root() / "knowledge", root / "knowledge")
        (root / "fixtures").mkdir(parents=True)
        (root / "fixtures" / "DATA_CLASS.json").write_text(json.dumps({"class": "synthetic"}))
        pseudonymizer = Pseudonymizer(SIMULATION_KEY)
        report = import_export(RawExportAdapter(directory / "export"), root, pseudonymizer)
        if report.rejected:
            raise ValueError(f"scenario import rejected: {report.rejected}")
        adapter = ReadOnlyDataAdapter(root)
        if getattr(llm, "name", None) == "anthropic":
            from hpc_agent.llm.claude import cloud_data_problem

            problem = cloud_data_problem(adapter.data_class())
            if problem:
                raise PermissionError(problem)
        for scenario_id in report.imported:
            label = labels[scenario_id]
            case = Case(case_id=scenario_id, description=f"simulated fault: {label.fault}",
                        label_source=label.label_source,
                        expected=Expectation(classification=label.classification, outcome=label.outcome,
                                             leading_categories=label.leading_categories,
                                             forbidden_categories=label.forbidden_categories,
                                             required_missing=label.required_missing))
            trace = TraceRecorder()
            started = time.monotonic()
            try:
                run = run_diagnosis(adapter.load_incident(scenario_id), build_default_gateway(adapter), scopes, llm=llm,
                                    trace=trace)
            except Exception as error:  # noqa: BLE001 - a failed case is recorded and scored as wrong
                results.append(_failed(case, error))
                logs.append(LogResult(case_id=scenario_id, fault=label.fault, total_lines=label.total_lines,
                                      key_lines=len(label.key_lines), key_lines_cited=0, log_searches=0,
                                      evidence=EVIDENCE.get(label.fault, ""),
                                      error=f"{type(error).__name__}: {str(error)[:200]}"))
                _keep(runs_dir, scenario_id, None, trace, time.monotonic() - started, logs[-1].error)
                continue
            _keep(runs_dir, scenario_id, run, trace, time.monotonic() - started, "")
            results.append(score(case, run))
            lead = next((c for c in run.rca.probable_causes if c.status.value == "hypothesis"), None)
            faulty = {_pseudonymize_component(c, pseudonymizer) for c in label.components}
            component_hit = (bool(lead) and bool(faulty & set(lead.components))) if faulty else None
            cited = _cited(run, pseudonymizer)
            wanted = {(source, None if source in SCHEDULER_LOGS else pseudonymizer.pseudonym("node", node), int(line))
                      for source, node, line in (key.split(":") for key in label.key_lines)}
            found = len(wanted & cited)
            logs.append(LogResult(case_id=scenario_id, fault=label.fault, total_lines=label.total_lines,
                                  key_lines=len(wanted), key_lines_cited=found,
                                  log_searches=sum(t.tool == "get_log_lines" for t in run.tools_invoked),
                                  log_analyses=sum(t.tool == "analyze_logs" for t in run.tools_invoked),
                                  consults=sum(d.consulted_by is not None for d in run.diagnoses),
                                  evidence=EVIDENCE.get(label.fault, ""), component_hit=component_hit,
                                  fallbacks=[f"{d.agent}: {d.reasoning_note}" for d in run.diagnoses
                                             if d.reasoning == "rules-fallback"]))
    return results, summarize(results), logs


def _pseudonymize_component(component: str, pseudonymizer: Pseudonymizer) -> str:
    kind, _, name = component.partition(":")
    return f"node:{pseudonymizer.pseudonym('node', name)}" if kind == "node" else component


def _keep(runs_dir: Path | None, scenario_id: str, run, trace: TraceRecorder, seconds: float, error: str) -> None:
    """<runs_dir>/<id>.run.json (the DiagnosticRun, or the error) and <id>.trace.jsonl (the agent trace)."""
    if runs_dir is None:
        return
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = {"scenario_id": scenario_id, "wall_seconds": round(seconds, 3), "error": error,
              "run": run.model_dump(mode="json") if run is not None else None}
    (runs_dir / f"{scenario_id}.run.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    with (runs_dir / f"{scenario_id}.trace.jsonl").open("w", encoding="utf-8") as handle:
        for event in trace.events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def write_name_map(directory: Path, scenario_id: str) -> Path:
    """<dir>/names/<id>.txt: each simulated node and the job ID with the pseudonym the importer gives it
    under SIMULATION_KEY. Only for simulated data (the key is public); lets a drill participant match
    an agent report (pseudonyms) to the raw files (simulated names). Line numbers are the same in both."""
    base = directory / "export" / scenario_id
    syslog = base / "logs" / "syslog"
    names = set(re.findall(r"^\S+ (\S+) ", syslog.read_text(encoding="utf-8"), re.MULTILINE)) if syslog.is_file() else set()
    names = {n for n in names if n.startswith("sim-")}
    names |= {p.stem for p in (base / "logs" / "slurmd").glob("*.log")}
    pseudonymizer = Pseudonymizer(SIMULATION_KEY)
    rows = [f"{name} = {pseudonymizer.pseudonym('node', name)}" for name in sorted(names)]
    job_id = json.loads((base / "incident.json").read_text(encoding="utf-8"))["job_id"]
    rows.append(f"job {job_id} = {pseudonymizer.pseudonym('job', job_id)}")
    path = directory / "names" / f"{scenario_id}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# simulated name = pseudonym (import with HPC_AGENT_PSEUDONYM_KEY set to the simulation key)\n"
                    + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def render_log_section(logs: list[LogResult], results: list | None = None) -> str:
    keyed = [r for r in logs if r.key_lines]
    cited = sum(r.key_lines_cited > 0 for r in keyed)
    lines = ["## Log evidence (simulated scenarios)", "",
             f"Scenarios where an agent cited at least one injected key log line: **{cited} of {len(keyed)}**. "
             "Rule-based agents never search logs (by design in this version), so in a rules run this is 0.", "",
             "| Scenario | Fault | Log lines | Key lines | Key lines cited | Log searches | Log analyses | Consulted agents "
             "| Faulty component named | Error |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    mark = {True: "yes", False: "**no**", None: "–"}
    lines += [f"| {r.case_id} | {r.fault} | {r.total_lines} | {r.key_lines} | {r.key_lines_cited} | {r.log_searches} "
              f"| {r.log_analyses} | {r.consults} | {mark[r.component_hit]} | {r.error or ''} |" for r in logs]
    scored = [r for r in logs if r.component_hit is not None]
    failed = [r for r in logs if r.error]
    lines += ["", "**Reading the numbers.** Top-1 and top-3 above count a match of the root-cause *category* "
              "(for example `node.unhealthy`), not of the specific mechanism. \"Faulty component named\" checks "
              f"whether the leading hypothesis names the labelled node or OST: **{sum(r.component_hit for r in scored)} "
              f"of {len(scored)}** scenarios that have one. Mechanism, timing and evidence relevance need a blinded "
              "human review with a fixed rubric. "
              f"Failed cases (scored as wrong, not dropped): **{len(failed)} of {len(logs)}**."]
    kinds = sorted({r.evidence for r in logs if r.evidence})
    if kinds and results:
        top1 = {r.case_id: r.leading_correct for r in results}
        lines += ["", "Novel faults by where their evidence is (a rules miss on a logs-only fault is a design limit):", "",
                  "| Evidence | Scenarios | Top-1 correct |", "|---|---|---|"]
        for kind in kinds:
            ids = [r.case_id for r in logs if r.evidence == kind]
            lines.append(f"| {kind} | {len(ids)} | {sum(bool(top1.get(i)) for i in ids)} |")
    return "\n".join(lines) + "\n"