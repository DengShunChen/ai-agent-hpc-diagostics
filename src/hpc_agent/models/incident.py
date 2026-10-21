"""Normalized incident model."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

INCIDENT_ID_PATTERN = r"^[A-Z0-9][A-Z0-9-]{2,63}$"

IncidentId = Annotated[str, StringConstraints(pattern=INCIDENT_ID_PATTERN)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Incident(BaseModel):
    """An incident or alert as reported. Its contents are claims to investigate, not verified facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incident_id: IncidentId
    timestamp: datetime | None = None
    system: NonEmptyStr = "HPC"
    node: NonEmptyStr | None = None
    job_id: NonEmptyStr | None = None
    severity: Severity
    symptoms: list[NonEmptyStr] = Field(default_factory=list)
    alerts: list[NonEmptyStr] = Field(default_factory=list)
    logs: list[NonEmptyStr] = Field(default_factory=list)
    operator_notes: list[NonEmptyStr] = Field(default_factory=list)
