"""Triage output."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent.models.diagnosis import Domain, Evidence
from hpc_agent.models.incident import IncidentId, NonEmptyStr


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: IncidentId
    classification: Domain
    related_domains: list[Domain] = Field(default_factory=list)
    # Which incident items (evidence sources) suggested each domain.
    domain_signals: dict[Domain, list[NonEmptyStr]] = Field(default_factory=dict)
    selected_agents: list[NonEmptyStr] = Field(default_factory=list)
    symptoms: list[NonEmptyStr] = Field(default_factory=list)
    available_evidence: list[Evidence] = Field(default_factory=list)
    missing_evidence: list[NonEmptyStr] = Field(default_factory=list)
