"""The simulated cluster: nodes, one incident job, the state the command outputs show, and log lines.

Fault and noise code (catalog.py, noise.py) only add log lines and change this state; render.py
turns it into files. Every random choice goes through `World.rng`, so a seed reproduces a scenario
byte for byte.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from hpc_agent.simulate.platform import PLATFORMS, Platform
from hpc_agent.simulate.scenario import Scenario


@dataclass
class Line:
    when: datetime
    source: str  # slurmctld | slurmd | pjm | syslog
    node: str
    text: str  # without the timestamp/host prefix; render.py adds the source's prefix
    key: bool = False  # shows the injected root cause


@dataclass
class Job:
    job_id: int
    name: str
    partition: str
    nodes: list[str]
    start: datetime
    end: datetime
    timelimit: timedelta
    state: str = "COMPLETED"
    exit_code: str = "0:0"  # sacct ExitCode, "exit:signal"
    uid: int = 20001


@dataclass
class Gpu:
    index: int
    pci: str
    health: str = "Healthy"
    temperature_c: int = 48
    dbe: int = 0
    message: str = ""


@dataclass
class Port:
    state: str = "Active"
    physical: str = "LinkUp"
    link_downed: int = 0
    symbol_errors: int = 0
    rcv_errors: int = 0
    xmit_discards: int = 0


@dataclass
class World:
    sc: Scenario
    p: Platform
    rng: random.Random
    cpu_nodes: list[str]
    gpu_nodes: list[str]
    osts: list[str]
    job: Job
    incident_time: datetime
    lines: list[Line] = field(default_factory=list)
    # sinfo: node -> (state, reason or "", time the reason was set)
    node_state: dict[str, tuple[str, str, datetime | None]] = field(default_factory=dict)
    ost_used: dict[str, int] = field(default_factory=dict)
    gpus: dict[str, list[Gpu]] = field(default_factory=dict)
    ports: dict[str, Port] = field(default_factory=dict)
    user_report: str = ""
    alerts: list[str] = field(default_factory=list)
    export_sacct: bool = True

    @property
    def nodes(self) -> list[str]:
        return self.cpu_nodes + self.gpu_nodes

    @property
    def end(self) -> datetime:
        return self.sc.start + timedelta(hours=self.sc.hours)

    def add(self, when: datetime, source: str, node: str, text: str, key: bool = False) -> None:
        if self.sc.start <= when <= self.end:
            self.lines.append(Line(when, source, node, text, key))

    def jitter(self, base: datetime, low: float, high: float) -> datetime:
        """base + a random offset in seconds, with millisecond precision."""
        return base + timedelta(milliseconds=self.rng.randint(int(low * 1000), int(high * 1000)))

    def pid(self) -> int:
        return self.rng.randint(1200, 99999)


PCI_SLOTS = ("0000:17:00", "0000:3b:00", "0000:86:00", "0000:af:00", "0000:18:00", "0000:3c:00", "0000:87:00",
             "0000:b0:00")


def build_world(sc: Scenario) -> World:
    rng = random.Random(sc.seed)
    p = PLATFORMS[sc.platform]
    cpu = [f"{p.cpu_prefix}-{i:04d}" for i in range(1, sc.nodes + 1)]
    gpu = [f"{p.gpu_prefix}-{i:04d}" for i in range(1, sc.gpu_nodes + 1)]
    osts = [f"OST{i:04X}" for i in range(sc.osts)]
    gpu_fault = sc.fault.startswith("gpu_")
    pool = gpu if gpu_fault else cpu
    # GPU jobs run on one node (the gpu_health dataset describes one node); CPU jobs on 2-4.
    size = 1 if gpu_fault else rng.randint(2, min(4, len(pool) - 1))
    first = rng.randrange(0, len(pool) - size + 1)
    nodes = pool[first:first + size]
    # The job ends around 60-80% of the window; the ticket comes in 5-40 minutes later.
    end = sc.start + timedelta(hours=sc.hours) * rng.uniform(0.6, 0.8)
    end = end.replace(microsecond=rng.randint(0, 999) * 1000)
    timelimit = timedelta(hours=rng.choice((4, 6, 8, 12)))
    start = end - timelimit * rng.uniform(0.25, 0.7)
    job = Job(job_id=rng.randint(7_000_000, 7_999_999), name=rng.choice(("cfd_run", "md_sim", "train_llm", "wrf_fc",
                                                                         "lammps_run", "bert_ft")),
              partition=p.gpu_partition if gpu_fault else p.cpu_partition, nodes=nodes, start=start, end=end, timelimit=timelimit)
    world = World(sc=sc, p=p, rng=rng, cpu_nodes=cpu, gpu_nodes=gpu, osts=osts, job=job,
                  incident_time=end + timedelta(minutes=rng.randint(5, 40)))
    for node in world.nodes:
        world.node_state[node] = ("IDLE", "", None)
        world.ports[node] = Port(symbol_errors=rng.choice((0, 0, 0, 1, 2)))
    for node in gpu:
        world.gpus[node] = [Gpu(i, PCI_SLOTS[i], temperature_c=rng.randint(38, 62)) for i in range(4)]
    for ost in osts:
        world.ost_used[ost] = rng.randint(50, 62)  # Lustre's allocator keeps OSTs fairly balanced
    return world
