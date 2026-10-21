"""Specialist-agent diagnosis and RCA result models.

These models keep FACT (evidence), HYPOTHESIS (possible/probable causes),
RECOMMENDATION and MISSING INFORMATION in separate fields so they can never be
blended into one unverifiable narrative.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent.models.incident import IncidentId, NonEmptyStr

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


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


class AgentDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(pattern=r"^[a-z][a-z0-9-]*-agent$")
    domain: Domain
    finding: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    possible_causes: list[NonEmptyStr] = Field(default_factory=list)
    recommended_checks: list[NonEmptyStr] = Field(default_factory=list)
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    missing_information: list[NonEmptyStr] = Field(default_factory=list)
    confidence: Confidence = 0.0
    human_approval_required: Literal[True] = True

    @model_validator(mode="after")
    def _claims_need_evidence(self) -> AgentDiagnosis:
        if self.finding.strip() and not self.evidence:
            raise ValueError("a finding requires at least one supporting evidence item")
        if self.confidence > 0.0 and not self.evidence:
            raise ValueError("non-zero confidence requires supporting evidence")
        return self


class HypothesisStatus(StrEnum):
    HYPOTHESIS = "hypothesis"
    CONFIRMED = "confirmed"
    RULED_OUT = "ruled_out"


class ProbableCause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: NonEmptyStr
    status: HypothesisStatus = HypothesisStatus.HYPOTHESIS
    supporting_evidence: list[Evidence] = Field(default_factory=list)
    conflicting_evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = 0.0

    @model_validator(mode="after")
    def _confirmation_needs_tool_evidence(self) -> ProbableCause:
        if self.confidence > 0.0 and not self.supporting_evidence:
            raise ValueError("non-zero confidence requires supporting evidence")
        if self.status is HypothesisStatus.CONFIRMED:
            # Reported symptoms alone can never confirm a root cause.
            if not any(e.kind is EvidenceKind.TOOL_RESULT for e in self.supporting_evidence):
                raise ValueError("a confirmed cause requires tool-derived supporting evidence")
            if self.conflicting_evidence:
                raise ValueError("a cause with conflicting evidence cannot be confirmed")
        return self


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
    confidence: Confidence = 0.0
    human_approval_required: Literal[True] = True

    @model_validator(mode="after")
    def _consistency(self) -> RCAResult:
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
