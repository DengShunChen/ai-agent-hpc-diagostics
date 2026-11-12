"""Result of one diagnostic workflow run: triage, specialist diagnoses and the RCA."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent.models.audit import ToolInvocationRecord
from hpc_agent.models.diagnosis import AgentDiagnosis, Evidence, RCAResult, RemediationPlan
from hpc_agent.models.incident import IncidentId, NonEmptyStr
from hpc_agent.models.triage import TriageResult


class CascadeRecord(BaseModel):
    """How a cascade run went: the rules ran first; the LLM agents only when the rules led with an outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    escalated: bool
    reason: NonEmptyStr
    rules_lead: str | None = None  # category the rules led with
    ai_lead: str | None = None  # checked cause category the LLM agents added, when they found one
    rules_seconds: float = Field(ge=0)
    ai_seconds: float | None = Field(default=None, ge=0)


class DiagnosticRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyStr
    incident_id: IncidentId
    start_time: datetime
    end_time: datetime
    triage: TriageResult
    diagnoses: list[AgentDiagnosis] = Field(default_factory=list)
    # Agents triage selected that are not implemented; their domains are unexamined.
    unavailable_agents: list[NonEmptyStr] = Field(default_factory=list)
    rca: RCAResult
    remediation: RemediationPlan | None = None  # None only for runs recorded before the remediation agent
    tools_invoked: list[ToolInvocationRecord] = Field(default_factory=list)
    # Set when this run re-diagnoses an incident after a rejection or revision request.
    supersedes: NonEmptyStr | None = None
    # Site whose profile was used, and when the incident was reported (for workflow metrics).
    site: NonEmptyStr = "default"
    incident_reported_at: datetime | None = None
    cascade: CascadeRecord | None = None  # set only by workflows/cascade.py
    human_approval_required: Literal[True] = True

    @property
    def root_cause_determined(self) -> bool:
        return self.rca.confirmed_root_cause is not None

    @property
    def facts(self) -> list[Evidence]:
        return self.rca.facts

    @property
    def missing_information(self) -> list[str]:
        return self.rca.missing_information

    @property
    def recommended_checks(self) -> list[str]:
        return self.rca.recommended_checks
