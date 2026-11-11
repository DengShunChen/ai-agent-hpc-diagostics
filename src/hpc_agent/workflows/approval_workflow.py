"""Human-in-the-loop approval workflow and audit trail.

    diagnose -> DRAFT -> DIAGNOSIS_COMPLETE -> AWAITING_HUMAN_APPROVAL
    human:     APPROVED -> CLOSED
               REJECTED | REVISION_REQUESTED -> REVISE -> new run (supersedes the old one)

APPROVED records a human decision only. This module has no way to execute an action,
and agents never receive a reference to it: only the human-facing CLI/API calls decide().
State lives under a writable state directory, separate from the read-only data roots:
    runs/<run_id>.json   write-once run snapshots
    approvals.jsonl      hash-chained approval transitions
    audit.jsonl          hash-chained AuditRecords
    explanations.jsonl   hash-chained LLM explanations (context only; never change a run)
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from collections.abc import Mapping
from pathlib import Path

from hpc_agent.data_adapter import ALLOWED_DIRS, ReadOnlyDataAdapter, default_data_root
from hpc_agent.llm.explainer import Explanation, explain
from hpc_agent.llm.skill_writer import DraftRejected, write_draft
from hpc_agent.llm.provider import ChatLLM, LLMProvider
from hpc_agent.logging.audit_logger import AppendOnlyLog
from hpc_agent.models import (
    ActionForReview,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
    AuditRecord,
    DiagnosticRun,
    HypothesisStatus,
    Incident,
)
from hpc_agent.models.approval import HUMAN_DECISION_STATUSES, AgentReasoning, is_human_operator
from hpc_agent.safety.approval import InvalidTransitionError, transition
from hpc_agent.safety.policy import Operation, SafetyPolicy
from hpc_agent.tools.gateway import ToolGateway
from hpc_agent.runtime.skills import SkillError, SkillLibrary
from hpc_agent.runtime.trace import TraceRecorder
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis

S = ApprovalStatus
_RUN_ID = re.compile(r"run-[0-9a-f]{12}")
COMMENT_REQUIRED = frozenset({S.REJECTED, S.REVISION_REQUESTED})
NOT_EXECUTED_NOTE = "APPROVED records a human decision only; this platform executes nothing."


class UnknownRunError(KeyError):
    def __str__(self) -> str:
        return f"unknown run: {self.args[0]!r}"


def default_state_dir() -> Path:
    override = os.environ.get("HPC_AGENT_STATE_DIR")
    return Path(override) if override else default_data_root() / "audit-data"


class RunStore:
    """Write-once JSON snapshots of diagnostic runs."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise UnknownRunError(run_id)
        return self._dir / f"{run_id}.json"

    def save(self, run: DiagnosticRun) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path(run.run_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never overwrite
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(run.model_dump_json(indent=2))

    def load(self, run_id: str) -> DiagnosticRun:
        path = self._path(run_id)
        if not path.is_file():
            raise UnknownRunError(run_id)
        return DiagnosticRun.model_validate_json(path.read_text(encoding="utf-8"))


def build_approval_request(run: DiagnosticRun, status: ApprovalStatus) -> ApprovalRequest:
    rca = run.rca
    open_causes = [c for c in rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]
    actions = []
    for action in rca.recommended_actions:
        related = [c.confidence for c in open_causes if set(c.supporting_evidence) & set(action.supporting_evidence)]
        actions.append(
            ActionForReview(
                proposed_action=action.action,
                reason=action.reason,
                supporting_evidence=action.supporting_evidence,
                risk=action.risk,
                affected_component=action.affected_component,
                confidence=max(related, default=rca.confidence),
                missing_information=rca.missing_information,
                verification=action.verification,
                rollback=action.rollback,
                reference=action.reference,
            )
        )
    note = NOT_EXECUTED_NOTE
    if not actions:
        note += (" No remediation action is proposed: a decision accepts or rejects the assessment and its "
                 "recommended checks.")
    return ApprovalRequest(
        run_id=run.run_id,
        incident_id=run.incident_id,
        status=status,
        classification=rca.classification,
        summary=rca.summary,
        confidence=rca.confidence,
        hypotheses=[f"({c.domain}, {c.confidence:.2f}) {c.cause}" for c in open_causes],
        missing_information=rca.missing_information,
        recommended_checks=rca.recommended_checks,
        actions=actions,
        remediation_summary=run.remediation.summary if run.remediation else "",
        deferred=[f"({d.confidence:.2f}) {d.cause}: {d.reason}" for d in run.remediation.deferred]
        if run.remediation else [],
        agents=[AgentReasoning(agent="triage-agent", reasoning=run.triage.reasoning, note=run.triage.reasoning_note),
                *(AgentReasoning(agent=d.agent, reasoning=d.reasoning,
                                 note=d.reasoning_note + (f"; consulted by {d.consulted_by}" if d.consulted_by else ""))
                  for d in run.diagnoses)],
        discarded_proposals=[f"{d.agent}: {p}" for d in run.diagnoses for p in d.discarded_proposals],
        note=note,
    )


class ApprovalService:
    def __init__(
        self,
        state_dir: Path,
        adapter: ReadOnlyDataAdapter,
        gateway: ToolGateway,
        scopes: Mapping[str, frozenset[str]] | None = None,
        reasoning_llm: ChatLLM | None = None,
        skills_root: Path | None = None,
    ) -> None:
        resolved = state_dir.resolve()
        data_root = adapter.root
        if any(resolved.is_relative_to((data_root / d).resolve()) for d in ALLOWED_DIRS):
            raise ValueError("state directory must not be inside a read-only data root")
        self._adapter = adapter
        self._gateway = gateway
        self._scopes = scopes
        self.reasoning_llm = reasoning_llm  # None: rule-based agents
        self.runs = RunStore(resolved / "runs")
        self.approvals = AppendOnlyLog(resolved / "approvals.jsonl")
        self.audit = AppendOnlyLog(resolved / "audit.jsonl")
        self.explanations = AppendOnlyLog(resolved / "explanations.jsonl")
        self.skill_drafts = AppendOnlyLog(resolved / "skill-drafts.jsonl")
        self._skills_root = skills_root or data_root  # an approved skill goes to <skills_root>/skills/
        self._traces = resolved / "traces"

    # --- diagnosis -----------------------------------------------------------------
    def diagnose(self, incident: Incident, supersedes: str | None = None) -> DiagnosticRun:
        trace = TraceRecorder()
        run = run_diagnosis(incident, self._gateway, self._scopes, llm=self.reasoning_llm, trace=trace)
        if supersedes:
            run = run.model_copy(update={"supersedes": supersedes})
        self.runs.save(run)
        traces = AppendOnlyLog(self._traces / f"{run.run_id}.jsonl")
        for event in trace.events:
            traces.append(event)
        record = ApprovalRecord(run_id=run.run_id, incident_id=run.incident_id, status=S.DRAFT)
        self._record_transition(record)
        for status in (S.DIAGNOSIS_COMPLETE, S.AWAITING_HUMAN_APPROVAL):
            record = transition(record, status)
            self._record_transition(record)
        rca = run.rca
        self.audit.append(
            AuditRecord(
                run_id=run.run_id,
                incident_id=run.incident_id,
                start_time=run.start_time,
                end_time=run.end_time,
                workflow_stage=S.AWAITING_HUMAN_APPROVAL,
                agents_invoked=["triage-agent", *(d.agent for d in run.diagnoses), "rca-coordinator"],
                tools_invoked=run.tools_invoked,
                evidence_used=[e.source for e in rca.facts] + [r.source for r in rca.references],
                diagnosis=rca.summary,
                recommendations=[*rca.recommended_checks, *(a.action for a in rca.recommended_actions)],
                approval_status=S.AWAITING_HUMAN_APPROVAL,
                operator_comment=f"supersedes {supersedes}" if supersedes else "",
            ).model_dump(mode="json")
        )
        return run

    def incident_ids(self) -> list[str]:
        return self._adapter.list_incident_ids()

    def load_incident(self, incident_id: str) -> Incident:
        return self._adapter.load_incident(incident_id)

    # --- review --------------------------------------------------------------------
    def history(self, run_id: str) -> list[ApprovalRecord]:
        records = [ApprovalRecord.model_validate(r) for r in self.approvals.records() if r["run_id"] == run_id]
        if not records:
            raise UnknownRunError(run_id)
        if records[0].status is not S.DRAFT:
            raise InvalidTransitionError(f"{run_id}: history does not start at DRAFT")
        for previous, current in zip(records, records[1:]):
            transition(previous, current.status, current.operator, current.operator_comment)  # re-validate
        return records

    def overview(self) -> list[ApprovalRecord]:
        """Latest approval record of every run, most recently started first."""
        latest: dict[str, ApprovalRecord] = {}
        for raw in self.approvals.records():
            latest[raw["run_id"]] = ApprovalRecord.model_validate(raw)
        return list(reversed(latest.values()))

    def status(self, run_id: str) -> ApprovalRecord:
        return self.history(run_id)[-1]

    def trace(self, run_id: str) -> list[dict]:
        """The run's agent trace (empty for rule-based runs), after verifying its hash chain."""
        self.runs.load(run_id)  # validates the id and that the run exists
        return AppendOnlyLog(self._traces / f"{run_id}.jsonl").records()

    def verify_traces(self) -> tuple[int, int]:
        """(trace files, events) after verifying every trace's hash chain."""
        files = sorted(self._traces.glob("run-*.jsonl")) if self._traces.is_dir() else []
        return len(files), sum(AppendOnlyLog(path).verify() for path in files)

    def review(self, run_id: str) -> ApprovalRequest:
        return build_approval_request(self.runs.load(run_id), self.status(run_id).status)

    # --- LLM explanation (optional) ---------------------------------------------------
    def explain(self, run_id: str, provider: LLMProvider, operator: str | None = None) -> Explanation:
        """Ask the local LLM to explain an existing run. Changes no run or approval state."""
        SafetyPolicy().require(Operation.GENERATE_EXPLANATION)
        run = self.runs.load(run_id)
        status = self.status(run_id).status
        result = explain(run, provider)
        audit = AuditRecord(
            run_id=run_id,
            incident_id=run.incident_id,
            start_time=result.generated_at,
            end_time=result.generated_at,
            workflow_stage=status,
            approval_status=status,
            agents_invoked=["evidence-explainer"],
            diagnosis=f"LLM explanation {result.status} ({result.provider}:{result.model})",
            operator=operator,
            operator_comment="; ".join(result.rejection_reasons)[:500],
        )
        self.explanations.append(result.model_dump(mode="json"))
        self.audit.append(audit.model_dump(mode="json"))
        return result

    def latest_explanation(self, run_id: str) -> Explanation | None:
        self.runs.load(run_id)  # validates the run id
        found = [r for r in self.explanations.records() if r.get("run_id") == run_id]
        return Explanation.model_validate(found[-1]) if found else None

    # --- skill drafts: the agent proposes, a human approves ------------------------------
    def draft_skill(self, run_id: str, provider: LLMProvider, operator: str | None = None) -> dict:
        """Ask the LLM to draft a skill from a run a human APPROVED. Never touches skills/."""
        SafetyPolicy().require(Operation.DRAFT_SKILL)
        run = self.runs.load(run_id)
        approved = [r for r in self.history(run_id) if r.status is S.APPROVED]
        if not approved or self.status(run_id).status not in (S.APPROVED, S.CLOSED):
            raise InvalidTransitionError(f"{run_id}: a skill can only be drafted from a run a human approved")
        record = {"event": "drafted", "draft_id": f"draft-{uuid.uuid4().hex[:12]}", "run_id": run_id,
                  "incident_id": run.incident_id, "requested_by": operator, "provider": provider.name,
                  "model": provider.model, "at": datetime.now(UTC).isoformat()}
        try:
            draft = write_draft(run, approved[-1].operator_comment, provider, SkillLibrary.load(self._skills_root))
            record |= {"name": draft.name, "agents": list(draft.agents), "text": draft.text}
        except DraftRejected as error:
            record |= {"event": "rejected_by_checks", "reasons": error.reasons}
        self.skill_drafts.append(record)
        self._audit_skill(record, operator, record.get("name") or "; ".join(record.get("reasons", []))[:500])
        return record

    def list_skill_drafts(self) -> list[dict]:
        """The latest state of each draft (drafted, approved, rejected or rejected_by_checks)."""
        latest: dict[str, dict] = {}
        for record in self.skill_drafts.records():
            latest[record["draft_id"]] = {**latest.get(record["draft_id"], {}), **record}
        return list(latest.values())

    def review_skill(self, draft_id: str, decision: str, operator: str, comment: str = "") -> dict:
        """A human approves (copies the draft into skills/) or rejects a pending draft."""
        SafetyPolicy().require(Operation.RECORD_HUMAN_DECISION)
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        if not is_human_operator(operator):
            raise ValueError("a skill can only be approved or rejected by a human operator")
        if decision == "rejected" and not comment.strip():
            raise ValueError("rejecting a draft requires a comment explaining why")
        draft = next((d for d in self.list_skill_drafts() if d["draft_id"] == draft_id), None)
        if draft is None or draft["event"] != "drafted":
            raise InvalidTransitionError(f"{draft_id}: no pending draft with that id")
        record = {"event": decision, "draft_id": draft_id, "operator": operator, "comment": comment,
                  "at": datetime.now(UTC).isoformat()}
        if decision == "approved":
            record["path"] = self._install_skill(draft)
        self.skill_drafts.append(record)
        self._audit_skill({**draft, **record}, operator, comment)
        return {**draft, **record}

    def _install_skill(self, draft: dict) -> str:
        domain = draft["agents"][0].removesuffix("-agent")
        target = self._skills_root / "skills" / domain / draft["name"] / "SKILL.md"
        if target.exists():
            raise InvalidTransitionError(f"skill {draft['name']!r} already exists")
        target.parent.mkdir(parents=True, exist_ok=False)
        target.write_text(draft["text"], encoding="utf-8")
        try:
            SkillLibrary.load(self._skills_root)  # the whole library must still load
        except SkillError:
            target.unlink()
            target.parent.rmdir()
            raise
        return target.relative_to(self._skills_root).as_posix()

    def _audit_skill(self, record: dict, operator: str | None, note: str) -> None:
        now = datetime.now(UTC)
        status = self.status(record["run_id"]).status
        self.audit.append(AuditRecord(
            run_id=record["run_id"], incident_id=record["incident_id"], start_time=now, end_time=now,
            workflow_stage=status, approval_status=status, agents_invoked=["skill-writer"],
            diagnosis=f"skill draft {record['draft_id']}: {record['event']}", operator=operator,
            operator_comment=note[:500],
        ).model_dump(mode="json"))

    # --- human decisions -----------------------------------------------------------
    def decide(self, run_id: str, decision: ApprovalStatus, operator: str, comment: str = "") -> ApprovalRecord:
        decision = S(decision)
        if decision not in HUMAN_DECISION_STATUSES:
            raise InvalidTransitionError(f"{decision} is not a human decision")
        if decision in COMMENT_REQUIRED and not comment.strip():
            raise ValueError(f"{decision} requires an operator comment explaining why")
        return self._advance(run_id, decision, operator, comment)

    def close(self, run_id: str, operator: str, comment: str = "") -> ApprovalRecord:
        return self._advance(run_id, S.CLOSED, operator, comment)

    def revise(self, run_id: str, operator: str, comment: str = "") -> DiagnosticRun:
        """Mark a rejected run for revision and re-diagnose the incident as a new run."""
        self._advance(run_id, S.REVISE, operator, comment)
        incident = self._adapter.load_incident(self.runs.load(run_id).incident_id)
        return self.diagnose(incident, supersedes=run_id)

    def _advance(self, run_id: str, status: ApprovalStatus, operator: str, comment: str) -> ApprovalRecord:
        if not operator or not is_human_operator(operator):
            raise ValueError(f"{status} requires a human operator identity")
        record = transition(self.status(run_id), status, operator, comment)
        audit = AuditRecord(
            run_id=run_id,
            incident_id=record.incident_id,
            start_time=record.timestamp,
            end_time=record.timestamp,
            workflow_stage=status,
            approval_status=status,
            operator=operator,
            operator_comment=comment,
        )  # validated (incl. credential scan) before anything is written
        self._record_transition(record)
        self.audit.append(audit.model_dump(mode="json"))
        return record

    def _record_transition(self, record: ApprovalRecord) -> None:
        self.approvals.append(record.model_dump(mode="json"))


def render_review(request: ApprovalRequest) -> str:
    lines = [
        f"APPROVAL REVIEW  run {request.run_id}  incident {request.incident_id}",
        f"STATUS: {request.status}",
        f"CLASSIFICATION: {request.classification}   OVERALL CONFIDENCE: {request.confidence:.2f}",
        "",
        "SUMMARY",
        f"  {request.summary}",
        "\nHYPOTHESES (unconfirmed)",
        *([f"  - {h}" for h in request.hypotheses] or ["  none"]),
        "\nPROPOSED ACTIONS",
    ]
    for i, a in enumerate(request.actions, 1):
        lines += [
            f"  [{i}] Proposed Action:     {a.proposed_action}",
            f"      Reason:              {a.reason}",
            "      Supporting Evidence:",
            *[f"        - {e.statement}   <{e.source}>" for e in a.supporting_evidence],
            f"      Risk:                {a.risk}",
            f"      Affected Component:  {a.affected_component}",
            f"      Confidence:          {a.confidence:.2f}",
            "      Verify Before Acting:",
            *([f"        - {v}" for v in a.verification] or ["        none"]),
            f"      Rollback:            {a.rollback or 'n/a'}",
            f"      Reference:           {a.reference or 'n/a'}",
            "      Missing Information:",
            *([f"        - {m}" for m in a.missing_information] or ["        none"]),
        ]
    if not request.actions:
        lines.append("  none")
    lines.append("\nAGENT REASONING")
    lines += [f"  - {a.agent}: {a.reasoning}" + (f" ({a.note})" if a.note else "") for a in request.agents]
    if request.discarded_proposals:
        lines.append("  discarded model proposals (failed the deterministic checks; not findings):")
        lines += [f"    - {p}" for p in request.discarded_proposals]
    if request.remediation_summary:
        lines += ["\nREMEDIATION PLAN", f"  {request.remediation_summary}"]
        lines += [f"  deferred: {d}" for d in request.deferred]
    lines += [
        "\nMISSING INFORMATION",
        *([f"  - {m}" for m in request.missing_information] or ["  none"]),
        "\nRECOMMENDED CHECKS",
        *([f"  - {c}" for c in request.recommended_checks] or ["  none"]),
        "",
        f"NOTE: {request.note}",
    ]
    return "\n".join(lines)
