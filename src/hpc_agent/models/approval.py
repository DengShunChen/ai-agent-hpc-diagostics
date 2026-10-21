"""Human approval record.

APPROVED only records a human decision. It never executes the proposed action.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hpc_agent.models.incident import IncidentId, NonEmptyStr


class ApprovalStatus(StrEnum):
    DRAFT = "DRAFT"
    DIAGNOSIS_COMPLETE = "DIAGNOSIS_COMPLETE"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    REVISE = "REVISE"
    CLOSED = "CLOSED"


# Statuses that represent a decision only a human operator may make.
HUMAN_DECISION_STATUSES = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.REVISION_REQUESTED}
)

_OPERATOR_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,63}$")
# POC-level guard against AI/system identities. Real authentication comes in a later iteration.
_NON_HUMAN_IDENTITY = re.compile(
    r"(^|[-_.])(agent|coordinator|bot|system|ai|llm|assistant|automation|claude|gpt)([-_.]|$)",
    re.IGNORECASE,
)


def is_human_operator(operator: str) -> bool:
    return bool(_OPERATOR_PATTERN.match(operator)) and not _NON_HUMAN_IDENTITY.search(operator)


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: NonEmptyStr
    incident_id: IncidentId
    status: ApprovalStatus
    operator: str | None = None
    operator_comment: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("timestamp")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _human_decision(self) -> ApprovalRecord:
        if self.status in HUMAN_DECISION_STATUSES:
            if not self.operator or not is_human_operator(self.operator):
                raise ValueError(f"{self.status} requires a human operator identity")
        return self
