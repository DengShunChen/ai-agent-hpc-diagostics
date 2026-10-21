"""Result of one diagnostic workflow run: triage, specialist diagnoses and the RCA."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent.models.audit import ToolInvocationRecord
from hpc_agent.models.diagnosis import AgentDiagnosis, Evidence, RCAResult
from hpc_agent.models.incident import IncidentId, NonEmptyStr
from hpc_agent.models.triage import TriageResult


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
    tools_invoked: list[ToolInvocationRecord] = Field(default_factory=list)
    # Set when this run re-diagnoses an incident after a rejection or revision request.
    supersedes: NonEmptyStr | None = None
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
