"""Scenario specs and the labels derived from them.

A scenario names one injected root cause (a fault from the catalog), the misleading signals
to add (distractors), the cluster size and a seed. Labels come from the catalog entry of the
fault, not from looking at what an agent produced.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from hpc_agent.models.incident import INCIDENT_ID_PATTERN

Fault = Literal[
    "oom_kill", "segfault", "walltime", "admin_cancel", "memory_ecc_node_down",
    "gpu_xid79", "gpu_xid48", "gpu_xid13_app", "ib_flap_lustre", "ost_full", "ost_imbalance",
    "logs_missing",
    # novel: not encoded in the rule-based agents (evaluation/scenarios/novel)
    "slow_node_walltime", "link_degraded_mpi", "mds_slow_hang", "gpu_pcie_aer", "clock_skew_auth",
]
NOVEL_FAULTS: tuple[str, ...] = ("slow_node_walltime", "link_degraded_mpi", "mds_slow_hang", "gpu_pcie_aer",
                                 "clock_skew_auth")
ALL_FAULTS: tuple[str, ...] = Fault.__args__  # type: ignore[attr-defined]
# The known faults, in their original order: blind sets draw from this by default, so existing seeds
# reproduce the same scenarios.
FAULTS: tuple[str, ...] = tuple(f for f in ALL_FAULTS if f not in NOVEL_FAULTS)

Distractor = Literal[
    "other_node_xid13",       # another node logs a GPU application XID at the same time
    "old_correctable_ecc",    # the job's node logged correctable ECC errors hours earlier
    "recovered_link_flap",    # one IB link on a job node went down and came back hours earlier
    "other_job_oom",          # a different job was OOM-killed on a different node
    "lustre_reconnects",      # harmless Lustre reconnect messages across the cluster
    "drained_other_node",     # an unrelated node was drained for maintenance
]
DISTRACTORS: tuple[str, ...] = Distractor.__args__  # type: ignore[attr-defined]

LABEL_SOURCE = ("synthetic; root cause injected by the scenario generator (labels come from the fault "
                "catalog, written by the POC developer; not independent ground truth)")


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=INCIDENT_ID_PATTERN)
    seed: int = Field(ge=0)
    fault: Fault
    platform: Literal["slurm-x86", "tcs-fx1000"] = "slurm-x86"
    distractors: list[Distractor] = Field(default_factory=list)
    nodes: int = Field(default=16, ge=4, le=64)
    gpu_nodes: int = Field(default=4, ge=2, le=16)
    osts: int = Field(default=8, ge=4, le=32)
    start: AwareDatetime = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
    hours: float = Field(default=6.0, ge=1.0, le=24.0)
    noise_level: float = Field(default=1.0, ge=0.1, le=3.0)  # scales background log volume
    description: str = ""


class Labels(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    fault: str
    label_source: str = LABEL_SOURCE
    classification: list[str]
    outcome: Literal["supported_hypothesis", "insufficient_evidence", "no_supported_hypothesis"]
    leading_categories: list[str] = Field(default_factory=list)
    forbidden_categories: list[str] = Field(default_factory=list)
    required_missing: list[str] = Field(default_factory=list)
    # The injected lines that show the root cause: "source:node:line" (line numbers after rendering).
    key_lines: list[str] = Field(default_factory=list)
    total_lines: int = 0
    # The faulty infrastructure components ("node:<name>", "ost:<id>"). Empty for job or application faults
    # (OOM, segfault, walltime, cancel, application XID), where the node the job ran on is not at fault.
    components: list[str] = Field(default_factory=list)


def load_scenarios(directory: Path) -> list[Scenario]:
    return [Scenario.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
            for p in sorted(directory.glob("*.yaml"))]


def random_scenarios(seed: int, count: int, prefix: str = "SIM-BLIND", platform: str = "slurm-x86",
                     faults: tuple[str, ...] = FAULTS) -> list[Scenario]:
    """A blind set: faults (from `faults`, the known ones by default) and distractors drawn at random."""
    rng = random.Random(seed)
    out = []
    for i in range(count):
        out.append(Scenario(
            scenario_id=f"{prefix}-{i + 1:03d}",
            seed=rng.randrange(1, 2**31),
            fault=rng.choice(faults),
            platform=platform,
            distractors=rng.sample(DISTRACTORS, rng.randint(1, 3)),
            nodes=rng.choice((8, 16, 24, 32)),
            gpu_nodes=rng.choice((2, 4, 8)),
            osts=rng.choice((4, 8, 12)),
            start=datetime(2026, 3, 2, tzinfo=UTC) + timedelta(days=rng.randint(0, 60), hours=rng.randint(0, 23)),
            hours=rng.choice((4.0, 6.0, 8.0)),
        ))
    return out


def stratified_scenarios(seed: int, per_fault: int, faults: tuple[str, ...] = FAULTS,
                         platforms: tuple[str, ...] = ("slurm-x86",), prefix: str = "SIM-BLIND") -> list[Scenario]:
    """A blind set with exactly `per_fault` scenarios of every fault on every platform, in shuffled order.

    Everything else (seed, distractors, sizes, time) is drawn at random, so every fault type is in the
    denominator and no type is left out by chance."""
    rng = random.Random(seed)
    cells = [(platform, fault) for platform in platforms for fault in faults for _ in range(per_fault)]
    rng.shuffle(cells)
    return [Scenario(
        scenario_id=f"{prefix}-{i + 1:03d}",
        seed=rng.randrange(1, 2**31),
        fault=fault,
        platform=platform,
        distractors=rng.sample(DISTRACTORS, rng.randint(1, 3)),
        nodes=rng.choice((8, 16, 24, 32)),
        gpu_nodes=rng.choice((2, 4, 8)),
        osts=rng.choice((4, 8, 12)),
        start=datetime(2026, 3, 2, tzinfo=UTC) + timedelta(days=rng.randint(0, 60), hours=rng.randint(0, 23)),
        hours=rng.choice((4.0, 6.0, 8.0)),
    ) for i, (platform, fault) in enumerate(cells)]
