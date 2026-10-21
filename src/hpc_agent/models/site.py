"""Site profile: the diagnostic thresholds that differ between sites.

A profile can tune *diagnosis* only. It has no fields for the safety policy, the tool
allowlist, approval rules or execution, so a site cannot weaken those by configuration
(unknown fields are rejected). Bounds keep every value within a sane range; the tool-call
budget cannot be raised above 200.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SITE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"


class SiteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: str = Field(pattern=SITE_ID_PATTERN)
    description: str = ""
    scheduler: Literal["slurm", "pjm"] = "slurm"  # informational: which scheduler the site runs
    storage_full_pct: float = Field(default=95.0, ge=50.0, le=100.0)
    ost_imbalance_spread_pct: float = Field(default=20.0, ge=1.0, le=100.0)
    gpu_thermal_limit_c: float = Field(default=85.0, ge=40.0, le=120.0)
    correlation_window_minutes: int = Field(default=30, ge=1, le=1440)
    tool_call_budget: int = Field(default=40, ge=1, le=200)
    # Minimum confidence of a tool-backed hypothesis before remediation is proposed.
    # Floor 0.3: weak hypotheses never get remediation proposals, whatever the site sets.
    remediation_min_confidence: float = Field(default=0.4, ge=0.3, le=0.9)


DEFAULT_PROFILE = SiteProfile(site_id="default", description="Built-in defaults")
