"""Result of one diagnostic workflow run (Iteration 2: triage + specialist diagnoses)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent.models.audit import ToolInvocationRecord
from hpc_agent.models.diagnosis import AgentDiagnosis, Evidence
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
    # Agents triage selected that are not implemented yet; their domains are unexamined.
    unavailable_agents: list[NonEmptyStr] = Field(default_factory=list)
    tools_invoked: list[ToolInvocationRecord] = Field(default_factory=list)
    # No RCA Coordinator exists yet, so no run can claim a determined root cause.
    root_cause_determined: Literal[False] = False
    human_approval_required: Literal[True] = True

    @property
    def facts(self) -> list[Evidence]:
        seen: dict[str, Evidence] = {}
        for item in [*self.triage.available_evidence, *(e for d in self.diagnoses for e in d.evidence)]:
            seen.setdefault(f"{item.source}|{item.statement}", item)
        return list(seen.values())

    @property
    def missing_information(self) -> list[str]:
        items = [*self.triage.missing_evidence, *(m for d in self.diagnoses for m in d.missing_information)]
        items += [f"{agent} not available in this iteration: its domain was not examined"
                  for agent in self.unavailable_agents]
        return list(dict.fromkeys(items))

    @property
    def recommended_checks(self) -> list[str]:
        return list(dict.fromkeys(c for d in self.diagnoses for c in d.recommended_checks))
