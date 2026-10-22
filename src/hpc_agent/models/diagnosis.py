"""Specialist-agent diagnosis and RCA result models.

These models keep FACT (evidence), HYPOTHESIS (possible/probable causes),
RECOMMENDATION and MISSING INFORMATION in separate fields so they can never be
blended into one unverifiable narrative.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent.models.incident import IncidentId, NonEmptyStr

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
# How an agent reached its output: deterministic rules, a language model (whose proposals
# passed deterministic checks), or rules because the model was unavailable or unusable.
Reasoning = Literal["rules", "llm", "rules-fallback"]


class Domain(StrEnum):
    STORAGE = "storage"
    SCHEDULER = "scheduler"
    NETWORK = "network"
    GPU = "gpu"
    MULTI_DOMAIN = "multi-domain"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    INCIDENT_REPORT = "incident_report"  # stated in the incident (symptom, alert, log, operator note)
    TOOL_RESULT = "tool_result"  # returned by a Tool Gateway call
    KNOWLEDGE = "knowledge"  # SOP, manual or incident history


class Evidence(BaseModel):
    """A traceable statement of fact with the reference it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    source: NonEmptyStr  # e.g. "POC-STORAGE-001:symptoms[0]" or "tool:get_ost_usage"
    statement: NonEmptyStr
    observed_at: datetime | None = None  # when the observation was made, if the data says so

    @model_validator(mode="after")
    def _timezone_aware(self) -> Evidence:
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


def _no_knowledge(items: list[Evidence], field: str) -> None:
    if any(e.kind is EvidenceKind.KNOWLEDGE for e in items):
        raise ValueError(f"knowledge documents are references, not {field} about this incident")


def _only_knowledge(items: list[Evidence]) -> None:
    if any(e.kind is not EvidenceKind.KNOWLEDGE for e in items):
        raise ValueError("references may only contain knowledge-base items")


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecommendedAction(BaseModel):
    """A proposed action. Recording it never executes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: NonEmptyStr
    reason: NonEmptyStr
    risk: RiskLevel
    affected_component: NonEmptyStr
    supporting_evidence: list[Evidence] = Field(default_factory=list)
    # Set by the remediation agent's playbook:
    addresses: str | None = None  # category of the hypothesis this action responds to
    verification: list[NonEmptyStr] = Field(default_factory=list)  # checks a human does before acting
    rollback: str | None = None
    reference: str | None = None  # SOP id


class HypothesisStatus(StrEnum):
    HYPOTHESIS = "hypothesis"
    CONFIRMED = "confirmed"
    RULED_OUT = "ruled_out"


class ProbableCause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: NonEmptyStr
    domain: Domain | None = None
    # Stable label for comparison and evaluation, e.g. "storage.capacity", "gpu.application".
    category: str | None = Field(default=None, pattern=r"^[a-z]+(\.[a-z_]+)+$")
    # Components this cause concerns, e.g. "ost:OST0002", "node:sim-node-0042", "gpu:node/3".
    components: list[NonEmptyStr] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.HYPOTHESIS
    supporting_evidence: list[Evidence] = Field(default_factory=list)
    conflicting_evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = 0.0

    @model_validator(mode="after")
    def _confirmation_needs_tool_evidence(self) -> ProbableCause:
        if self.confidence > 0.0 and not self.supporting_evidence:
            raise ValueError("non-zero confidence requires supporting evidence")
        _no_knowledge(self.supporting_evidence + self.conflicting_evidence, "evidence")
        if self.status is HypothesisStatus.RULED_OUT and not self.conflicting_evidence:
            raise ValueError("a ruled-out cause must cite the conflicting evidence")
        if self.status is HypothesisStatus.CONFIRMED:
            # Reported symptoms alone can never confirm a root cause.
            if not any(e.kind is EvidenceKind.TOOL_RESULT for e in self.supporting_evidence):
                raise ValueError("a confirmed cause requires tool-derived supporting evidence")
            if self.conflicting_evidence:
                raise ValueError("a cause with conflicting evidence cannot be confirmed")
        return self


class AgentDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(pattern=r"^[a-z][a-z0-9-]*-agent$")
    domain: Domain
    finding: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    possible_causes: list[ProbableCause] = Field(default_factory=list)
    # Components implicated by evidence, e.g. "node:sim-node-0042", "ost:OST0002".
    affected_components: list[NonEmptyStr] = Field(default_factory=list)
    recommended_checks: list[NonEmptyStr] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    missing_information: list[NonEmptyStr] = Field(default_factory=list)
    # Knowledge-base material (SOPs, manuals, past incidents): context, never evidence.
    references: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = 0.0
    reasoning: Reasoning = "rules"
    reasoning_note: str = ""  # model used, or why the rules were used instead
    # Model proposals that failed the deterministic checks, with the reason (never shown as findings).
    discarded_proposals: list[NonEmptyStr] = Field(default_factory=list)
    human_approval_required: Literal[True] = True

    @model_validator(mode="after")
    def _claims_need_evidence(self) -> AgentDiagnosis:
        _no_knowledge(self.evidence, "evidence")
        _only_knowledge(self.references)
        if self.finding.strip() and not self.evidence:
            raise ValueError("a finding requires at least one supporting evidence item")
        if self.confidence > 0.0 and not self.evidence:
            raise ValueError("non-zero confidence requires supporting evidence")
        for cause in self.possible_causes:
            if cause.status is HypothesisStatus.CONFIRMED:
                raise ValueError("specialist agents may not confirm a root cause")
            cited = cause.supporting_evidence + cause.conflicting_evidence
            if any(e not in self.evidence for e in cited):
                raise ValueError("cause evidence must be part of the diagnosis evidence")
        return self

    @property
    def hypotheses(self) -> list[ProbableCause]:
        return [c for c in self.possible_causes if c.status is HypothesisStatus.HYPOTHESIS]


class RCAResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: IncidentId
    classification: Domain
    affected_domains: list[Domain] = Field(default_factory=list)
    summary: str = ""
    facts: list[Evidence] = Field(default_factory=list)
    probable_causes: list[ProbableCause] = Field(default_factory=list)
    supporting_evidence: list[Evidence] = Field(default_factory=list)
    conflicting_evidence: list[Evidence] = Field(default_factory=list)
    missing_information: list[NonEmptyStr] = Field(default_factory=list)
    recommended_checks: list[NonEmptyStr] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    cross_domain_relationships: list[NonEmptyStr] = Field(default_factory=list)
    # Timestamped observations in time order, for human review.
    timeline: list[Evidence] = Field(default_factory=list)
    references: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = 0.0
    human_approval_required: Literal[True] = True

    @model_validator(mode="after")
    def _consistency(self) -> RCAResult:
        _no_knowledge(self.facts + self.supporting_evidence + self.conflicting_evidence, "facts")
        _only_knowledge(self.references)
        if self.confidence > 0.0 and not (self.facts or self.supporting_evidence):
            raise ValueError("non-zero confidence requires facts or supporting evidence")
        confirmed = [c for c in self.probable_causes if c.status is HypothesisStatus.CONFIRMED]
        if len(confirmed) > 1:
            raise ValueError("at most one cause may be confirmed")
        return self

    @property
    def confirmed_root_cause(self) -> ProbableCause | None:
        return next(
            (c for c in self.probable_causes if c.status is HypothesisStatus.CONFIRMED), None
        )


class DeferredHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: NonEmptyStr
    category: str | None = None
    confidence: Confidence = 0.0
    reason: NonEmptyStr


class RemediationPlan(BaseModel):
    """The remediation agent's proposal. Proposals only: nothing here is ever executed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: Literal["remediation-agent"] = "remediation-agent"
    summary: NonEmptyStr
    min_confidence: Confidence
    deferred: list[DeferredHypothesis] = Field(default_factory=list)
    executes: Literal[False] = False
    human_approval_required: Literal[True] = True
