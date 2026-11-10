"""Evaluation harness: run labelled cases through the real workflow and score them.

Metrics (spec section 21):
  * correct domain classification
  * root cause accuracy: leading open hypothesis category (top-1) and top-3, and whether the
    outcome (supported / insufficient evidence / nothing supported) matches the label
  * evidence quality: share of open hypotheses backed by tool evidence; recall of the
    missing-information items the label requires
  * unsupported claim rate: confirmed causes, confident (>= 0.5) hypotheses without tool
    evidence, or forbidden categories, per open hypothesis
  * human override rate: from recorded human decisions only; never simulated
  * diagnostic processing time: wall-clock duration of each run

No MTTR figure is produced: MTTR can only be reported after measurement against
representative incidents in operation.
"""

from __future__ import annotations

import json
import shutil
import statistics
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent.data_adapter import ReadOnlyDataAdapter, default_data_root
from hpc_agent.logging.audit_logger import AppendOnlyLog
from hpc_agent.models import DiagnosticRun, EvidenceKind, HypothesisStatus, Incident
from hpc_agent.safety.secrets import assert_no_credentials
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.llm.provider import ChatLLM
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis

Outcome = Literal["supported_hypothesis", "insufficient_evidence", "no_supported_hypothesis"]
CONFIDENT = 0.5


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: list[str]
    outcome: Outcome
    leading_categories: list[str] = Field(default_factory=list)
    forbidden_categories: list[str] = Field(default_factory=list)
    required_missing: list[str] = Field(default_factory=list)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    description: str
    label_source: str
    fixture: str | None = None
    incident: dict | None = None
    test_data: dict[str, dict] = Field(default_factory=dict)
    expected: Expectation


class CaseResult(BaseModel):
    case_id: str
    description: str
    label_source: str
    classification: str
    classification_correct: bool
    expected_outcome: Outcome
    outcome: Outcome
    outcome_correct: bool
    leading_category: str | None
    leading_cause: str | None
    leading_confidence: float
    leading_correct: bool | None  # None when the label names no leading category
    top3_correct: bool | None
    open_hypotheses: int
    tool_backed_hypotheses: int
    unsupported_claims: list[str]
    missing_required: int
    missing_found: int
    facts_reported: int
    facts_tool: int
    references: int
    proposed_actions: int
    duration_ms: float
    # Agent reasoning in this run: e.g. {"llm": 3, "rules-fallback": 1}; discarded model proposals.
    reasoning: dict[str, int] = Field(default_factory=dict)
    discarded_proposals: int = 0


class Summary(BaseModel):
    cases: int
    classification_accuracy: float
    outcome_accuracy: float
    refusal_accuracy: float | None
    top1_root_cause_accuracy: float | None
    top3_root_cause_accuracy: float | None
    evidence_backed_hypothesis_rate: float | None
    missing_information_recall: float | None
    unsupported_claim_rate: float
    unsupported_claims: int
    human_override_rate: float | None
    human_decisions: int
    processing_ms_mean: float
    processing_ms_median: float
    processing_ms_max: float
    skills_version: str | None = None  # fingerprint of skills/ for LLM-driven runs (frozen per evaluation)


def load_cases(directory: Path) -> list[Case]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert_no_credentials(data, context=path.name)
        cases.append(Case.model_validate(data))
    return cases


def _outcome(run: DiagnosticRun) -> Outcome:
    open_causes = [c for c in run.rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]
    if not open_causes:
        return "no_supported_hypothesis"
    if any(e.kind is EvidenceKind.TOOL_RESULT for e in open_causes[0].supporting_evidence):
        return "supported_hypothesis"
    return "insufficient_evidence"


def _reasoning_counts(run: DiagnosticRun) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in [run.triage, *run.diagnoses]:
        counts[item.reasoning] = counts.get(item.reasoning, 0) + 1
    return counts


def score(case: Case, run: DiagnosticRun) -> CaseResult:
    exp = case.expected
    rca = run.rca
    open_causes = [c for c in rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]
    lead = open_causes[0] if open_causes else None

    unsupported = [f"confirmed: {c.cause}" for c in rca.probable_causes if c.status is HypothesisStatus.CONFIRMED]
    for c in open_causes:
        tool_backed = any(e.kind is EvidenceKind.TOOL_RESULT for e in c.supporting_evidence)
        if c.confidence >= CONFIDENT and not tool_backed:
            unsupported.append(f"confident without tool evidence: {c.cause}")
        if c.category in exp.forbidden_categories:
            unsupported.append(f"forbidden category {c.category}: {c.cause}")

    missing_text = " | ".join(rca.missing_information)
    categories = [c.category for c in open_causes]
    return CaseResult(
        case_id=case.case_id,
        description=case.description,
        label_source=case.label_source,
        classification=rca.classification,
        classification_correct=rca.classification in exp.classification,
        expected_outcome=exp.outcome,
        outcome=_outcome(run),
        outcome_correct=_outcome(run) == exp.outcome,
        leading_category=lead.category if lead else None,
        leading_cause=lead.cause if lead else None,
        leading_confidence=lead.confidence if lead else 0.0,
        leading_correct=(lead is not None and lead.category in exp.leading_categories) if exp.leading_categories else None,
        top3_correct=any(c in exp.leading_categories for c in categories[:3]) if exp.leading_categories else None,
        open_hypotheses=len(open_causes),
        tool_backed_hypotheses=sum(
            any(e.kind is EvidenceKind.TOOL_RESULT for e in c.supporting_evidence) for c in open_causes),
        unsupported_claims=unsupported,
        missing_required=len(exp.required_missing),
        missing_found=sum(item in missing_text for item in exp.required_missing),
        facts_reported=sum(e.kind is EvidenceKind.INCIDENT_REPORT for e in rca.facts),
        facts_tool=sum(e.kind is EvidenceKind.TOOL_RESULT for e in rca.facts),
        references=len(rca.references),
        proposed_actions=len(rca.recommended_actions),
        duration_ms=(run.end_time - run.start_time).total_seconds() * 1000,
        reasoning=_reasoning_counts(run),
        discarded_proposals=sum(len(d.discarded_proposals) for d in run.diagnoses),
    )


def _case_root(case: Case, workdir: Path, project_root: Path) -> tuple[ReadOnlyDataAdapter, Incident]:
    """An isolated data root for one case; POC fixtures are read from the project as-is."""
    if case.fixture:
        adapter = ReadOnlyDataAdapter(project_root)
        return adapter, adapter.load_incident(case.fixture)
    root = workdir / case.case_id
    shutil.copytree(project_root / "knowledge", root / "knowledge")
    incident = Incident.model_validate(case.incident)
    (root / "fixtures" / "incidents").mkdir(parents=True)
    (root / "fixtures" / "incidents" / f"{incident.incident_id}.json").write_text(json.dumps(case.incident))
    if case.label_source.strip().lower().startswith("synthetic"):
        (root / "fixtures" / "DATA_CLASS.json").write_text(json.dumps({"class": "synthetic"}))
    for key, payload in case.test_data.items():
        domain, dataset = key.split("/")
        path = root / "test_data" / domain / incident.incident_id / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    adapter = ReadOnlyDataAdapter(root)
    return adapter, adapter.load_incident(incident.incident_id)


def human_override(approvals_log: Path | None) -> tuple[float | None, int]:
    """Share of recorded human decisions that rejected or sent back the AI assessment."""
    if approvals_log is None or not approvals_log.exists():
        return None, 0
    decisions = [r["status"] for r in AppendOnlyLog(approvals_log).records()
                 if r["status"] in ("APPROVED", "REJECTED", "REVISION_REQUESTED")]
    if not decisions:
        return None, 0
    overrides = sum(d != "APPROVED" for d in decisions)
    return overrides / len(decisions), len(decisions)


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(results: list[CaseResult], approvals_log: Path | None = None) -> Summary:
    override, decisions = human_override(approvals_log)
    hypotheses = sum(r.open_hypotheses for r in results)
    unsupported = sum(len(r.unsupported_claims) for r in results)
    # Cases where the correct answer is to NOT present a tool-backed conclusion.
    refusals = [r.outcome_correct for r in results if r.expected_outcome != "supported_hypothesis"]
    required = sum(r.missing_required for r in results)
    durations = [r.duration_ms for r in results]
    return Summary(
        cases=len(results),
        classification_accuracy=_rate([r.classification_correct for r in results]),
        outcome_accuracy=_rate([r.outcome_correct for r in results]),
        refusal_accuracy=_rate(refusals),
        top1_root_cause_accuracy=_rate([r.leading_correct for r in results if r.leading_correct is not None]),
        top3_root_cause_accuracy=_rate([r.top3_correct for r in results if r.top3_correct is not None]),
        evidence_backed_hypothesis_rate=(sum(r.tool_backed_hypotheses for r in results) / hypotheses) if hypotheses else None,
        missing_information_recall=(sum(r.missing_found for r in results) / required) if required else None,
        unsupported_claim_rate=unsupported / hypotheses if hypotheses else 0.0,
        unsupported_claims=unsupported,
        human_override_rate=override,
        human_decisions=decisions,
        processing_ms_mean=statistics.fmean(durations),
        processing_ms_median=statistics.median(durations),
        processing_ms_max=max(durations),
    )


def evaluate(
    cases_dir: Path,
    scopes: Mapping[str, frozenset[str]] | None = None,
    project_root: Path | None = None,
    approvals_log: Path | None = None,
    llm: ChatLLM | None = None,
) -> tuple[list[CaseResult], Summary]:
    """With `llm`, the cases are diagnosed by the LLM-driven agents (rules check them)."""
    project_root = project_root or default_data_root()
    results = []
    with tempfile.TemporaryDirectory(prefix="hpc-agent-eval-") as tmp:
        for case in load_cases(cases_dir):
            adapter, incident = _case_root(case, Path(tmp), project_root)
            if getattr(llm, "name", None) == "anthropic":
                from hpc_agent.llm.claude import cloud_data_problem

                problem = cloud_data_problem(adapter.data_class())
                if problem:
                    raise PermissionError(f"{case.case_id}: {problem}")
            run = run_diagnosis(incident, build_default_gateway(adapter), scopes, llm=llm)
            results.append(score(case, run))
    summary = summarize(results, approvals_log)
    if llm is not None:
        from hpc_agent.runtime.skills import SkillLibrary

        summary = summary.model_copy(update={"skills_version": SkillLibrary.load(default_data_root()).version()})
    return results, summary
