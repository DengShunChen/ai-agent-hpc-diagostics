"""Structured, immutable audit records."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, model_validator

from hpc_agent.models.approval import ApprovalStatus
from hpc_agent.models.incident import IncidentId, NonEmptyStr
from hpc_agent.safety.secrets import assert_no_credentials


class ToolInvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: NonEmptyStr
    agent: NonEmptyStr | None = None
    result_status: NonEmptyStr
    evidence_reference: str | None = None
    duration_ms: NonNegativeFloat = 0.0


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyStr
    incident_id: IncidentId
    start_time: datetime
    end_time: datetime | None = None
    workflow_stage: ApprovalStatus = ApprovalStatus.DRAFT
    agents_invoked: list[NonEmptyStr] = Field(default_factory=list)
    tools_invoked: list[ToolInvocationRecord] = Field(default_factory=list)
    evidence_used: list[NonEmptyStr] = Field(default_factory=list)
    diagnosis: str = ""
    recommendations: list[NonEmptyStr] = Field(default_factory=list)
    approval_status: ApprovalStatus = ApprovalStatus.DRAFT
    operator: str | None = None
    operator_comment: str = ""

    @model_validator(mode="after")
    def _validate(self) -> AuditRecord:
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time must not precede start_time")
        assert_no_credentials(self.model_dump(mode="json"), context="AuditRecord")
        return self

    @property
    def duration_seconds(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds()
