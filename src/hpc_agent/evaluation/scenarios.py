"""Evaluate on simulated scenarios: raw logs and command outputs with an injected root cause.

Each scenario goes the way a site export would: the raw-export adapter parses it, the importer
validates, pseudonymizes and scans it into a temporary data root, and the normal workflow diagnoses
it. Labels are read from <dir>/labels/, which the adapter never sees. Scoring reuses harness.score;
on top of it, this records whether the agents cited the injected key log lines.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from hpc_agent.data_adapter import ReadOnlyDataAdapter, default_data_root
from hpc_agent.evaluation.harness import Case, CaseResult, Expectation, Summary, score, summarize
from hpc_agent.ingest.deidentify import Pseudonymizer
from hpc_agent.ingest.importer import import_export
from hpc_agent.ingest.raw_export import RawExportAdapter
from hpc_agent.llm.provider import ChatLLM
from hpc_agent.simulate.scenario import Labels
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


def evaluate_scenarios(
    directory: Path, scopes: Mapping[str, frozenset[str]] | None = None, llm: ChatLLM | None = None,
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
            run = run_diagnosis(adapter.load_incident(scenario_id), build_default_gateway(adapter), scopes, llm=llm)
            results.append(score(case, run))
            cited = _cited(run, pseudonymizer)
            wanted = {(source, None if source in SCHEDULER_LOGS else pseudonymizer.pseudonym("node", node), int(line))
                      for source, node, line in (key.split(":") for key in label.key_lines)}
            found = len(wanted & cited)
            logs.append(LogResult(case_id=scenario_id, fault=label.fault, total_lines=label.total_lines,
                                  key_lines=len(wanted), key_lines_cited=found,
                                  log_searches=sum(t.tool == "get_log_lines" for t in run.tools_invoked)))
    return results, summarize(results), logs


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


def render_log_section(logs: list[LogResult]) -> str:
    keyed = [r for r in logs if r.key_lines]
    cited = sum(r.key_lines_cited > 0 for r in keyed)
    lines = ["## Log evidence (simulated scenarios)", "",
             f"Scenarios where an agent cited at least one injected key log line: **{cited} of {len(keyed)}**. "
             "Rule-based agents never search logs (by design in this version), so in a rules run this is 0.", "",
             "| Scenario | Fault | Log lines | Key lines | Key lines cited | Log searches |", "|---|---|---|---|---|---|"]
    lines += [f"| {r.case_id} | {r.fault} | {r.total_lines} | {r.key_lines} | {r.key_lines_cited} | {r.log_searches} |"
              for r in logs]
    return "\n".join(lines) + "\n"
