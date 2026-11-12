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
from hpc_agent.safety.secrets import find_credential_like
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.cascade import run_cascade
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
    # Does the leading hypothesis name any labelled faulty component? None when the label names none.
    component_hit: bool | None = None
    platform: str = ""
    group: str = ""  # known | novel | missing-data
    # LLM runs: True when triage and every specialist ran on the model (no rules fallback). None for rules runs.
    model_complete: bool | None = None
    fallbacks: list[str] = []  # agents that fell back to rules, with the reason
    # Cascade runs only (None otherwise): did the rules hand the case to the LLM agents, and was the rules' own
    # lead correct (so a report can count right->wrong and wrong->right against the rules alone).
    escalated: bool | None = None
    rules_top1: bool | None = None
    ai_top1: bool | None = None  # cascade: the LLM agents' second opinion matches the label (None: no checked cause)
    rules_seconds: float | None = None
    ai_seconds: float | None = None


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


def _failed(case: Case, reason: str, seconds: float) -> CaseResult:
    """A case that did not finish: outcome "failed", wrong on every case-level metric, real time kept.
    Metrics computed over output hypotheses (evidence quality) have no hypotheses to count for it."""
    exp = case.expected
    return CaseResult(case_id=case.case_id, description=f"{case.description}; FAILED: {safe_text(reason, 80)}",
                      label_source=case.label_source, classification="error", classification_correct=False,
                      expected_outcome=exp.outcome, outcome="failed", outcome_correct=False, leading_category=None,
                      leading_cause=None, leading_confidence=0.0,
                      leading_correct=False if exp.leading_categories else None,
                      top3_correct=False if exp.leading_categories else None, open_hypotheses=0,
                      tool_backed_hypotheses=0, unsupported_claims=[], missing_required=len(exp.required_missing),
                      missing_found=0, facts_reported=0, facts_tool=0, references=0, proposed_actions=0,
                      duration_ms=seconds * 1000)


SENSITIVE = "SENSITIVE INFORMATION DETECTED AND NOT PROCESSED."


def safe_text(text: str, limit: int = 200) -> str:
    """Error and fallback text as it may be written to records and reports: credential-like content withheld."""
    text = " ".join(str(text).split())[:limit]
    return SENSITIVE if find_credential_like(text) else text


def _masked(value):
    """Every string in a record, credential-like ones withheld (defense in depth for persisted runs)."""
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    if isinstance(value, str) and find_credential_like(value):
        return SENSITIVE
    return value


def safe_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {safe_text(str(error))}"


class BatchInvalid(ValueError):
    """The scenario directory itself is broken (not a failure of the system under test)."""


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


def manifest(directory: Path, llm: ChatLLM | None, cascade: bool = False) -> dict:
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
        "reasoning": "rules" if llm is None else "cascade" if cascade else "llm",
        "host": {"python": sys.version.split()[0], "platform": host_platform.platform(), "cpus": os.cpu_count()},
    }


def group_of(fault: str) -> str:
    return "missing-data" if fault == "logs_missing" else "novel" if fault in EVIDENCE else "known"


def evaluate_scenarios(
    directory: Path, scopes: Mapping[str, frozenset[str]] | None = None, llm: ChatLLM | None = None,
    runs_dir: Path | None = None, run_manifest: dict | None = None, cascade: bool = False,
) -> tuple[list[CaseResult], Summary, list[LogResult]]:
    """Every labelled scenario is a planned case; each one ends as a scored run or a recorded failure.

    With runs_dir (which must be new or empty): manifest.json is written before the first case
    (status "running") and finished as "complete" or "interrupted"; progress.jsonl gets one line per
    case as it finishes, so an interrupted batch still shows what ran."""
    if cascade and llm is None:
        raise BatchInvalid("a cascade run needs a model for the cases the rules hand over")
    labels = {p.stem: Labels.model_validate_json(p.read_text(encoding="utf-8"))
              for p in sorted((directory / "labels").glob("*.json"))}
    exports = {p.name for p in (directory / "export").iterdir() if p.is_dir()} if (directory / "export").is_dir() else set()
    if not labels or set(labels) != exports:
        raise BatchInvalid(f"evaluation batch invalid: labels and exports must match one to one "
                           f"(labels only: {sorted(set(labels) - exports)}, exports only: {sorted(exports - set(labels))})")
    if runs_dir is not None:
        if runs_dir.exists() and any(runs_dir.iterdir()):
            raise ValueError(f"runs directory must be new or empty: {runs_dir}")
        runs_dir.mkdir(parents=True, exist_ok=True)
    state = {**(run_manifest or {}), "status": "running", "planned_cases": sorted(labels)}
    _write_manifest(runs_dir, state)
    results, logs = [], []
    active: tuple | None = None
    batch_started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="hpc-agent-scenarios-") as tmp:
            root = Path(tmp) / "root"
            shutil.copytree(default_data_root() / "knowledge", root / "knowledge")
            (root / "fixtures").mkdir(parents=True)
            (root / "fixtures" / "DATA_CLASS.json").write_text(json.dumps({"class": "synthetic"}))
            pseudonymizer = Pseudonymizer(SIMULATION_KEY)
            import_started = time.monotonic()
            report = import_export(RawExportAdapter(directory / "export"), root, pseudonymizer)
            state["import_wall_seconds"] = round(time.monotonic() - import_started, 3)
            adapter = ReadOnlyDataAdapter(root)
            if getattr(llm, "name", None) == "anthropic":
                from hpc_agent.llm.claude import cloud_data_problem

                problem = cloud_data_problem(adapter.data_class())
                if problem:
                    raise PermissionError(problem)
            for scenario_id in sorted(labels):
                label = labels[scenario_id]
                case = Case(case_id=scenario_id, description=f"simulated fault: {label.fault}",
                            label_source=label.label_source,
                            expected=Expectation(classification=label.classification, outcome=label.outcome,
                                                 leading_categories=label.leading_categories,
                                                 forbidden_categories=label.forbidden_categories,
                                                 required_missing=label.required_missing))
                base = dict(case_id=scenario_id, fault=label.fault, total_lines=label.total_lines,
                            key_lines=len(label.key_lines), evidence=EVIDENCE.get(label.fault, ""),
                            platform=label.platform, group=group_of(label.fault),
                            component_hit=False if label.components else None,
                            model_complete=None if llm is None else False)  # a failed case did not complete
                trace, started = TraceRecorder(), time.monotonic()
                active = {"id": scenario_id, "trace": trace, "started": started, "kept": False}
                if scenario_id in report.rejected:
                    error = f"import rejected: {safe_text(report.rejected[scenario_id])}"
                    results.append(_failed(case, error, 0.0))
                    logs.append(LogResult(**base, key_lines_cited=0, log_searches=0, error=error))
                    _keep(runs_dir, scenario_id, None, trace, 0.0, error, llm)
                    continue
                try:
                    if cascade:
                        run = run_cascade(adapter.load_incident(scenario_id), build_default_gateway(adapter), llm,
                                          scopes, trace=trace)
                    else:
                        run = run_diagnosis(adapter.load_incident(scenario_id), build_default_gateway(adapter),
                                            scopes, llm=llm, trace=trace)
                except Exception as error:  # noqa: BLE001 - a failed case is recorded, never dropped
                    seconds = time.monotonic() - started
                    reason = safe_error(error)
                    results.append(_failed(case, reason, seconds))
                    logs.append(LogResult(**base, key_lines_cited=0, log_searches=0, error=reason))
                    _keep(runs_dir, scenario_id, None, trace, seconds, reason, llm)
                    continue
                _keep(runs_dir, scenario_id, run, trace, time.monotonic() - started, "", llm)
                active["kept"] = True  # its run is on disk; an interruption from here on must not rewrite it
                results.append(score(case, run))
                lead = next((c for c in run.rca.probable_causes if c.status.value == "hypothesis"), None)
                faulty = {_pseudonymize_component(c, pseudonymizer) for c in label.components}
                cited = _cited(run, pseudonymizer)
                wanted = {(source, None if source in SCHEDULER_LOGS else pseudonymizer.pseudonym("node", node), int(line))
                          for source, node, line in (key.split(":") for key in label.key_lines)}
                fallbacks = ([f"triage-agent: {safe_text(run.triage.reasoning_note)}"]
                             if run.triage.reasoning == "rules-fallback" else []) + [
                    f"{d.agent}: {safe_text(d.reasoning_note)}" for d in run.diagnoses if d.reasoning == "rules-fallback"]
                recorded = {f.split(":", 1)[0] for f in fallbacks}  # cross-check: the trace is the ground truth
                fallbacks += [f"{agent}: fell back in the trace but not in the run record"
                              for agent in sorted({e["agent"] for e in trace.events if e["kind"] == "fallback"} - recorded)]
                c = run.cascade
                logs.append(LogResult(**{**base, "component_hit": (bool(lead) and bool(faulty & set(lead.components)))
                                         if faulty else None,
                                         "model_complete": None if llm is None or (c and not c.escalated)
                                         else not fallbacks},
                                      escalated=c.escalated if c else None,
                                      rules_top1=(c.rules_lead in label.leading_categories
                                                  if label.leading_categories else None) if c else None,
                                      ai_top1=(c.ai_lead in label.leading_categories
                                               if c.ai_lead and label.leading_categories else None) if c else None,
                                      rules_seconds=c.rules_seconds if c else None,
                                      ai_seconds=c.ai_seconds if c else None,
                                      key_lines_cited=len(wanted & cited),
                                      log_searches=sum(t.tool == "get_log_lines" for t in run.tools_invoked),
                                      log_analyses=sum(t.tool == "analyze_logs" for t in run.tools_invoked),
                                      consults=sum(d.consulted_by is not None for d in run.diagnoses),
                                      fallbacks=fallbacks))
                active = None
    except BaseException as error:
        # The manifest is closed out first, so a problem while keeping the active case can't leave it "running".
        state["status"] = "interrupted"
        _write_manifest(runs_dir, {**state, "finished_cases": len(results), "interruption": safe_error(error),
                                   "active_case": active["id"] if active else None,
                                   "active_case_stage": None if not active else
                                   ("diagnosed, not scored" if active["kept"] else "diagnosing"),
                                   "pending_cases": sorted(labels)[len(results) + (1 if active else 0):],
                                   "batch_wall_seconds": round(time.monotonic() - batch_started, 3)})
        if active is not None and runs_dir is not None:
            seconds = time.monotonic() - active["started"]
            if active["kept"]:  # the run record stays as written; only the progress log notes the interruption
                _progress(runs_dir, active["id"], "interrupted after diagnosis", seconds, llm)
            else:  # keep what the case had: its trace, time and the usage known so far
                _keep(runs_dir, active["id"], None, active["trace"], seconds, safe_error(error), llm,
                      status="interrupted")
        raise
    state["status"] = "complete"
    _write_manifest(runs_dir, {**state, "finished_cases": len(results),
                               "failed_cases": sum(bool(r.error) for r in logs),
                               "batch_wall_seconds": round(time.monotonic() - batch_started, 3),
                               "timing_note": "per-case wall_seconds are the diagnosis stage only; the batch import "
                                              "is import_wall_seconds; batch_wall_seconds covers the whole batch"})
    return results, summarize(results), logs


def _write_manifest(runs_dir: Path | None, state: dict) -> None:
    if runs_dir is not None:
        state = {**state, "updated_at": datetime.now(UTC).isoformat()}
        (runs_dir / "manifest.json").write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _pseudonymize_component(component: str, pseudonymizer: Pseudonymizer) -> str:
    kind, _, name = component.partition(":")
    return f"node:{pseudonymizer.pseudonym('node', name)}" if kind == "node" else component


def _keep(runs_dir: Path | None, scenario_id: str, run, trace: TraceRecorder, seconds: float, error: str,
          llm: ChatLLM | None = None, status: str | None = None) -> None:
    """<id>.run.json (the DiagnosticRun, or the error), <id>.trace.jsonl, and a line in progress.jsonl."""
    if runs_dir is None:
        return
    record_path = runs_dir / f"{scenario_id}.run.json"
    if record_path.exists():
        raise FileExistsError(f"refusing to overwrite {record_path}")
    status = status or ("failed" if error else "completed")
    record = _masked({"scenario_id": scenario_id, "status": status, "wall_seconds": round(seconds, 3), "error": error,
                      "run": run.model_dump(mode="json") if run is not None else None})
    record_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    with (runs_dir / f"{scenario_id}.trace.jsonl").open("x", encoding="utf-8") as handle:
        for event in trace.events:
            handle.write(json.dumps(_masked(event), ensure_ascii=False, default=str) + "\n")
    _progress(runs_dir, scenario_id, status, seconds, llm)


def _progress(runs_dir: Path, scenario_id: str, status: str, seconds: float, llm: ChatLLM | None) -> None:
    usage = dict(getattr(llm, "usage", {}) or {}) if getattr(llm, "name", None) == "anthropic" else None
    with (runs_dir / "progress.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"scenario_id": scenario_id, "status": status,
                                 "wall_seconds": round(seconds, 3), "claude_usage": usage,
                                 "at": datetime.now(UTC).isoformat()}) + "\n")


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


def _cascade_section(rows: list[LogResult], by_id: dict, planned: int) -> list[str]:
    """Escalation rate, time per path, and the cascade against the rules alone on the same cases."""
    escalated = [r for r in rows if r.escalated]
    pairs = [(r.rules_top1, by_id[r.case_id].leading_correct) for r in rows if r.rules_top1 is not None]
    second = [r for r in escalated if r.rules_top1 is not None]

    def median(values: list[float]) -> str:
        values = sorted(values)
        return f"{values[len(values) // 2]:.3f} s" if values else "–"

    return ["", "### Cascade (rules first, LLM agents only where the rules lead with an outcome)", "",
            f"Escalated to the LLM agents: **{len(escalated)} of {len(rows)}** cases that ran ({planned} planned; "
            "failed cases have no cascade record). Median time: rules "
            f"{median([r.rules_seconds for r in rows if r.rules_seconds is not None])}, LLM agents on escalated cases "
            f"{median([r.ai_seconds for r in escalated if r.ai_seconds is not None])}.", "",
            "Category top-1 against the rules alone on the same cases (the rules' lead stays first, so a right-to-wrong "
            "flip is a defect):",
            "", "| Rules right, cascade right | Rules wrong, cascade right | Rules right, cascade **wrong** "
            "| Both wrong |", "|---|---|---|---|",
            f"| {sum(a and b for a, b in pairs)} | {sum(not a and b for a, b in pairs)} "
            f"| {sum(a and not b for a, b in pairs)} | {sum(not a and not b for a, b in pairs)} |", "",
            "The LLM agents' second opinion (their top checked cause, listed right below the rules' lead) on the "
            "escalated cases with a labelled category:", "",
            "| Rules wrong, **second opinion right** | Rules wrong, second opinion wrong | Rules right, second opinion "
            "right | Rules right, second opinion wrong | No checked cause |", "|---|---|---|---|---|",
            f"| {sum(r.rules_top1 is False and r.ai_top1 is True for r in second)} "
            f"| {sum(r.rules_top1 is False and r.ai_top1 is False for r in second)} "
            f"| {sum(r.rules_top1 is True and r.ai_top1 is True for r in second)} "
            f"| {sum(r.rules_top1 is True and r.ai_top1 is False for r in second)} "
            f"| {sum(r.ai_top1 is None for r in second)} |"]


def render_log_section(logs: list[LogResult], results: list | None = None) -> str:
    keyed = [r for r in logs if r.key_lines]
    cited = sum(r.key_lines_cited > 0 for r in keyed)
    lines = ["## Log evidence (simulated scenarios)", "",
             f"Scenarios where an agent cited at least one injected key log line: **{cited} of {len(keyed)}**. "
             "Rule-based agents never search logs; the scheduler agent quotes the warning and error lines on the job's nodes "
             "while it ran (`get_log_digest`, fixed terms), and those quotes count here. No hypothesis is made from them.", "",
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
              f"Failed cases (outcome \"failed\", counted in every case-level denominator, never dropped): "
              f"**{len(failed)} of {len(logs)}**. Metrics over output hypotheses (evidence quality) have no "
              "hypotheses from a failed case."]
    if results:
        by_id = {r.case_id: r for r in results}

        def count(rows, test) -> str:
            scored = [x for x in (test(r) for r in rows) if x is not None]
            return f"{sum(scored)}/{len(scored)}" if scored else "–"

        lines += ["", "### Counts by group and platform", "",
                  "Numerator/denominator. Category = the root-cause category matched (not the mechanism); component ="
                  " the leading hypothesis names any labelled faulty component (infrastructure faults only; the "
                  "metadata-server fault has no component label). Failed cases are in every denominator.", "",
                  "| Group | Platform | Cases | Category top-1 | Category top-3 | Component | Outcome | Failed "
                  "| Model completed (no rules fallback) |", "|---|---|---|---|---|---|---|---|---|"]
        for group in ("known", "novel", "missing-data"):
            for platform in sorted({r.platform for r in logs}):
                rows = [r for r in logs if r.group == group and r.platform == platform]
                if not rows:
                    continue
                lines.append(f"| {group} | {platform or '?'} | {len(rows)} "
                             f"| {count(rows, lambda r: by_id[r.case_id].leading_correct)} "
                             f"| {count(rows, lambda r: by_id[r.case_id].top3_correct)} "
                             f"| {count(rows, lambda r: r.component_hit)} "
                             f"| {count(rows, lambda r: by_id[r.case_id].outcome_correct)} "
                             f"| {sum(bool(r.error) for r in rows)} "
                             f"| {count(rows, lambda r: r.model_complete)} |")
        llm_rows = [r for r in logs if r.model_complete is not None]
        if llm_rows:
            clean = [r for r in llm_rows if r.model_complete]
            lines += ["", f"Model completion: **{len(clean)} of {len(llm_rows)}** planned cases ran with no agent falling "
                      "back to rules (failed cases count as not completed). On the completed cases only, category top-1: "
                      f"**{count(clean, lambda r: by_id[r.case_id].leading_correct)}** (a conditional subset, not a "
                      "replacement for the rows above, which include fallback and failed cases)."]
    cascaded = [r for r in logs if r.escalated is not None]
    if cascaded and results:
        lines += _cascade_section(cascaded, {r.case_id: r for r in results}, len(logs))
    kinds = sorted({r.evidence for r in logs if r.evidence})
    if kinds and results:
        top1 = {r.case_id: r.leading_correct for r in results}
        lines += ["", "Novel faults by where their evidence is (a rules miss on a logs-only fault is a design limit):", "",
                  "| Evidence | Scenarios | Top-1 correct |", "|---|---|---|"]
        for kind in kinds:
            ids = [r.case_id for r in logs if r.evidence == kind]
            lines.append(f"| {kind} | {len(ids)} | {sum(bool(top1.get(i)) for i in ids)} |")
    return "\n".join(lines) + "\n"