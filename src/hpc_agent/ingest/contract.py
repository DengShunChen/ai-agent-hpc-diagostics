"""Data contract for site exports: the saved-data shapes the diagnostic agents read.

A site adapter must produce exactly these shapes. Unknown fields are rejected, so an
export cannot carry fields nobody reviewed. Counters and percentages are bounded, and
timestamps must have a timezone. A missing value stays missing (None); nothing is filled in.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Pct = Annotated[float, Field(ge=0, le=100)]
Count = Annotated[int, Field(ge=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FilesystemUsage(_Strict):
    filesystem: Name
    used_pct: Pct | None = None
    health: Name | None = None
    collected_at: AwareDatetime | None = None


class Ost(_Strict):
    ost: Name
    used_pct: Pct | None = None
    state: Name | None = None


class OstUsage(_Strict):
    osts: list[Ost] = Field(min_length=1)
    collected_at: AwareDatetime | None = None


class JobStatus(_Strict):
    job_id: Name
    scheduler: Name | None = None
    state: Name | None = None
    exit_code: int | None = None
    requested_walltime: Name | None = None
    elapsed: Name | None = None
    end_time: AwareDatetime | None = None
    allocated_nodes: list[Name] = Field(default_factory=list)
    termination_reason: Text | None = None


class Node(_Strict):
    node: Name
    state: Name | None = None
    reason: Text | None = None
    state_changed_at: AwareDatetime | None = None


class NodeStatus(_Strict):
    nodes: list[Node] = Field(min_length=1)


class Member(_Strict):
    interface: Name
    state: Name | None = None


class Interface(_Strict):
    node: Name
    interface: Name
    state: Name | None = None
    rx_errors: Count | None = None
    tx_errors: Count | None = None
    link_flaps: Count | None = None
    last_change: AwareDatetime | None = None
    members: list[Member] = Field(default_factory=list)


class NetworkStatus(_Strict):
    interfaces: list[Interface] = Field(min_length=1)


class XidEvent(_Strict):
    xid: Count
    count: Annotated[int, Field(ge=1)] | None = None
    timestamp: AwareDatetime | None = None


class Gpu(_Strict):
    index: Count
    health: Name | None = None
    xid_events: list[XidEvent] = Field(default_factory=list)
    temperature_c: Annotated[float, Field(ge=-50, le=200)] | None = None
    power_w: Annotated[float, Field(ge=0, le=5000)] | None = None
    ecc_dbe_volatile: Count | None = None
    pcie_replay_count: Count | None = None
    nvlink_errors: Count | None = None


class GpuHealth(_Strict):
    node: Name
    format: Text | None = None
    gpus: list[Gpu] = Field(min_length=1)


# (domain, dataset) -> contract. These are the only datasets the Tool Gateway reads.
DATASETS: dict[tuple[str, str], type[_Strict]] = {
    ("storage", "filesystem_usage"): FilesystemUsage,
    ("storage", "ost_usage"): OstUsage,
    ("scheduler", "job_status"): JobStatus,
    ("scheduler", "node_status"): NodeStatus,
    ("network", "network_status"): NetworkStatus,
    ("gpu", "gpu_health"): GpuHealth,
}
