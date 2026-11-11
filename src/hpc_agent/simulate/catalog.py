"""Fault catalog: what each injected root cause leaves in the logs and command outputs, and its labels.

Each fault ends the incident job in its own way and adds the lines an engineer would find. Lines
that show the root cause are marked `key=True`; follow-on symptoms that don't name the cause are not.
The labels (expected domain, leading categories, forbidden categories) are part of the catalog, so
they are fixed before any agent runs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from hpc_agent.simulate.world import World


def hostlist(nodes: list[str]) -> str:
    """Slurm-style compressed node list for one prefix: sim-node-[0003-0005,0009]."""
    if len(nodes) == 1:
        return nodes[0]
    prefix = nodes[0].rsplit("-", 1)[0]
    numbers = sorted(int(n.rsplit("-", 1)[1]) for n in nodes)
    ranges, run = [], [numbers[0]]
    for n in numbers[1:]:
        if n == run[-1] + 1:
            run.append(n)
        else:
            ranges.append(run)
            run = [n]
    ranges.append(run)
    parts = [f"{r[0]:04d}" if len(r) == 1 else f"{r[0]:04d}-{r[-1]:04d}" for r in ranges]
    return f"{prefix}-[{','.join(parts)}]"


def launcher(w: World) -> str:
    return "mpiexec" if w.p.name == "tcs-fx1000" else "srun"


def stamp(when) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S")


# --- job lifecycle -------------------------------------------------------------------------------

def sched(w: World, when, text: str, key: bool = False) -> None:
    """A line in the job manager's log (slurmctld or PJM)."""
    w.add(when, w.p.scheduler_source, w.p.controller, text, key)


def node_log(w: World, when, node: str, text: str, key: bool = False) -> None:
    """A line in the per-node scheduler daemon log (slurmd); TCS exports none, so it is dropped there."""
    if w.p.node_source:
        w.add(when, w.p.node_source, node, text, key)


def job_start(w: World) -> None:
    j = w.job
    sched(w, j.start, w.p.allocate(j.job_id, hostlist(j.nodes), len(j.nodes), j.partition))
    for i, node in enumerate(j.nodes):
        t = w.jitter(j.start, 0.2, 1.5)
        if i == 0:
            node_log(w, t, node, f"Launching batch job {j.job_id} for UID {j.uid}")
            node_log(w, w.jitter(t, 0.01, 0.2), node,
                     f"[{j.job_id}.batch] task/cgroup: _memcg_initialize: job: alloc=256000MB mem.limit=256000MB "
                     "memsw.limit=unlimited")
        node_log(w, w.jitter(t, 5, 20), node, f"launch task StepId={j.job_id}.0 request from UID:{j.uid} GID:{j.uid}")


def job_end(w: World, how: str) -> None:
    """how: 'exit' or 'signal'; exit_code is 'exit:signal'."""
    j = w.job
    code, sig = j.exit_code.split(":")
    for k, text in enumerate(w.p.complete(j.job_id, how, code, sig)):
        sched(w, j.end if k == 0 else w.jitter(j.end, 0.001, 0.01), text)
    for node in j.nodes:
        node_log(w, w.jitter(j.end, 0.5, 3), node, f"[{j.job_id}.batch] done with job" if node == j.nodes[0]
                 else f"[{j.job_id}.0] done with job")


# --- faults --------------------------------------------------------------------------------------

def oom_kill(w: World) -> dict:
    j, node = w.job, w.rng.choice(w.job.nodes)
    j.state, j.exit_code = "OUT_OF_MEMORY", "0:125"
    t, pid = j.end - timedelta(seconds=w.rng.randint(2, 6)), w.pid()
    app = w.rng.choice(("python3", "pw.x", "lmp", "wrf.exe"))
    w.add(t, "syslog", node, f"kernel: {app} invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=0")
    w.add(w.jitter(t, 0.01, 0.05), "syslog", node,
          f"kernel: oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=step_0,mems_allowed=0-1,"
          f"oom_memcg=/{"pjm" if w.p.name == "tcs-fx1000" else "slurm"}/uid_{j.uid}/job_{j.job_id}/step_0,task={app},pid={pid},uid={j.uid}", key=True)
    w.add(w.jitter(t, 0.05, 0.1), "syslog", node,
          f"kernel: Memory cgroup out of memory: Killed process {pid} ({app}) total-vm:263012344kB, "
          f"anon-rss:255981220kB, file-rss:10240kB, shmem-rss:0kB, UID:{j.uid} pgtables:502312kB oom_score_adj:0",
          key=True)
    node_log(w, w.jitter(t, 1, 2), node,
             f"[{j.job_id}.0] error: Detected 1 oom_kill event in StepId={j.job_id}.0. Some of the step tasks have "
             "been OOM Killed.", key=True)
    if w.p.memory_limit(j.job_id, node):
        sched(w, w.jitter(t, 1, 2), w.p.memory_limit(j.job_id, node), key=True)
    job_start(w)
    job_end(w, "signal")
    w.user_report = w.rng.choice((
        f"job {j.job_id} died after a few hours, the output just stops",
        f"job {j.job_id} failed; output ends with '{launcher(w)}: error: {node}: task 0: Killed'",
    ))
    return dict(classification=["scheduler"], outcome="supported_hypothesis",
                leading=["scheduler.signal_termination"], forbidden=["scheduler.walltime", "node.unhealthy"])


def segfault(w: World) -> dict:
    j, node = w.job, w.rng.choice(w.job.nodes)
    j.state, j.exit_code = "FAILED", "139:0"
    j.end = j.start + timedelta(minutes=w.rng.randint(2, 15), seconds=w.rng.randint(0, 59))
    app = w.rng.choice(("solver", "cfd_main", "md_engine"))
    w.add(j.end - timedelta(seconds=1), "syslog", node,
          f"kernel: {app}[{w.pid()}]: segfault at 0 ip 00005581c2a1f3b4 sp 00007ffd3c9e8a10 error 4 in "
          f"{app}[5581c2a00000+3f000]", key=True)
    job_start(w)
    job_end(w, "exit")
    w.user_report = w.rng.choice((
        f"job {j.job_id} terminated shortly after start",
        f"job {j.job_id} failed: '{launcher(w)}: error: {node}: task 3: Segmentation fault (core dumped)'",
    ))
    return dict(classification=["scheduler"], outcome="supported_hypothesis",
                leading=["application.crash"], forbidden=["node.unhealthy", "scheduler.walltime"])


def walltime(w: World) -> dict:
    j = w.job
    j.start = j.end - j.timelimit
    j.state, j.exit_code = "TIMEOUT", "0:15"
    sched(w, j.end - timedelta(milliseconds=300), w.p.time_limit(j.job_id), key=True)
    for node in j.nodes:
        node_log(w, w.jitter(j.end, 0.1, 0.5), node,
              f"[{j.job_id}.0] *** STEP {j.job_id}.0 ON {node} CANCELLED AT {stamp(j.end)} DUE TO TIME LIMIT ***",
              key=node == j.nodes[0])
    job_start(w)
    job_end(w, "signal")
    w.user_report = f"job {j.job_id} was killed before it finished writing results"
    return dict(classification=["scheduler"], outcome="supported_hypothesis",
                leading=["scheduler.walltime"], forbidden=["node.unhealthy"])


def admin_cancel(w: World) -> dict:
    j = w.job
    j.state, j.exit_code = "CANCELLED by 0", "0:15"
    sched(w, j.end - timedelta(milliseconds=200), w.p.admin_delete(j.job_id), key=True)
    node_log(w, w.jitter(j.end, 0.1, 0.4), j.nodes[0],
          f"[{j.job_id}.batch] *** JOB {j.job_id} ON {j.nodes[0]} CANCELLED AT {stamp(j.end)} ***", key=True)
    job_start(w)
    job_end(w, "signal")
    w.user_report = f"job {j.job_id} disappeared from the queue, I did not cancel it"
    return dict(classification=["scheduler"], outcome="supported_hypothesis",
                leading=["scheduler.signal_termination"], forbidden=["scheduler.walltime", "node.unhealthy"])


def memory_ecc_node_down(w: World) -> dict:
    j, node = w.job, w.rng.choice(w.job.nodes)
    j.state, j.exit_code = "NODE_FAIL", "0:0"
    t = j.end - timedelta(seconds=w.rng.randint(60, 150))
    dimm = f"CPU_SrcID#{w.rng.randint(0, 1)}_MC#0_Chan#{w.rng.randint(0, 3)}_DIMM#0"
    if w.p.name == "tcs-fx1000":
        for k, text in enumerate(w.p.memory_ue()):
            w.add(w.jitter(t, 0.001 * k, 0.001 * k + 0.01), "syslog", node, text, key=k > 0)
        reason = "hardware error: uncorrectable memory error"
    else:
        w.add(t, "syslog", node, "kernel: mce: [Hardware Error]: Machine check events logged", key=False)
        w.add(w.jitter(t, 0.01, 0.1), "syslog", node,
              f"kernel: EDAC MC1: 1 UE memory read error on {dimm} (channel:2 slot:0 page:0x8a3f12 offset:0x0 "
              "grain:32 - err_code:0x0000:0x009f socket:1 imc:0 rank:0 bg:1 ba:2 row:0x1a3f col:0x3c0)", key=True)
        reason = "NHC: check_hw_eccerr: uncorrectable ECC errors"
        w.add(w.jitter(t, 20, 40), "syslog", node, f"nhc[{w.pid()}]: ERROR:  nhc:  Health check failed:  "
              "check_hw_eccerr:  ECC uncorrectable errors detected", key=True)
    reason_lines = w.p.node_reason(node, reason)
    sched(w, w.jitter(t, 40, 50), reason_lines[0], key=True)
    for text in reason_lines[1:]:
        sched(w, w.jitter(t, 50, 55), text)
    down = w.p.node_down(node, j.job_id)
    sched(w, j.end - timedelta(seconds=2), down[0])
    sched(w, j.end, down[1])
    w.node_state[node] = ("DOWN*", reason, t + timedelta(seconds=45))
    w.alerts.append(f"monitoring: node {node} is DOWN")
    job_start(w)
    if w.p.name != "tcs-fx1000":
        sched(w, w.jitter(j.end, 0.001, 0.01), f"_job_complete: JobId={j.job_id} done")
    w.user_report = f"job {j.job_id} failed with NODE_FAIL" if w.p.name != "tcs-fx1000" else \
        f"job {j.job_id} ended abnormally; pjstat shows it was killed"
    return dict(classification=["scheduler", "multi-domain"], outcome="supported_hypothesis",
                leading=["node.unhealthy"], forbidden=["application.crash", "scheduler.walltime"])


def _gpu_fault(w: World, xid: int, health: str, text: str, hardware: bool) -> tuple[str, int]:
    j, node = w.job, w.rng.choice(w.job.nodes)
    gpu = w.rng.randrange(4)
    pci, pid = w.gpus[node][gpu].pci, w.pid()
    t = j.end - timedelta(seconds=w.rng.randint(3, 20))
    w.add(t, "syslog", node, f"kernel: NVRM: Xid (PCI:{pci}): {xid}, pid={pid}, name=python3, {text}", key=True)
    w.gpus[node][gpu].health = health
    w.gpus[node][gpu].message = f"XID {xid} reported" if hardware else ""
    j.state, j.exit_code = "FAILED", "1:0"
    job_start(w)
    job_end(w, "exit")
    return node, gpu


def gpu_xid79(w: World) -> dict:
    node, gpu = _gpu_fault(w, 79, "Failure", "GPU has fallen off the bus.", True)
    pci = w.gpus[node][gpu].pci
    w.add(w.jitter(w.job.end, -15, -2), "syslog", node, f"kernel: NVRM: GPU {pci}.0: GPU has fallen off the bus.",
          key=True)
    w.gpus[node][gpu].temperature_c = 0
    w.user_report = f"training job {w.job.job_id} crashed: 'RuntimeError: CUDA error: unspecified launch failure'"
    return dict(classification=["gpu", "multi-domain"], outcome="supported_hypothesis",
                leading=["gpu.hardware"], forbidden=["gpu.application"])


def gpu_xid48(w: World) -> dict:
    node, gpu = _gpu_fault(w, 48, "Failure", "An uncorrectable double bit error (DBE) has been detected on GPU in the "
                           "framebuffer at partition 6, subpartition 0.", True)
    w.gpus[node][gpu].dbe = 1
    pci = w.gpus[node][gpu].pci
    w.add(w.jitter(w.job.end, -2, -1), "syslog", node,
          f"kernel: NVRM: Xid (PCI:{pci}): 63, pid='<unknown>', name=<unknown>, Row Remapper: New row "
          "(0x00000000000a3f12) marked for remapping, reset gpu to activate.")
    w.user_report = f"job {w.job.job_id} failed: 'CUDA error: uncorrectable ECC error encountered'"
    return dict(classification=["gpu", "multi-domain"], outcome="supported_hypothesis",
                leading=["gpu.hardware"], forbidden=["gpu.application"])


def gpu_xid13_app(w: World) -> dict:
    node, gpu = _gpu_fault(w, 13, "Warning", "Graphics SM Warp Exception on (GPC 3, TPC 1, SM 0): Out Of Range "
                           "Address", False)
    pci = w.gpus[node][gpu].pci
    w.add(w.jitter(w.job.end, -2, -1), "syslog", node,
          f"kernel: NVRM: Xid (PCI:{pci}): 13, pid={w.pid()}, name=python3, Graphics Exception: ESR "
          "0x51e730=0x2010e 0x51e734=0x24 0x51e728=0xc81eb60 0x51e72c=0x1174", key=True)
    w.user_report = f"job {w.job.job_id} aborted: 'CUDA error: an illegal memory access was encountered'"
    return dict(classification=["gpu", "multi-domain"], outcome="supported_hypothesis",
                leading=["gpu.application"], forbidden=["gpu.hardware"])


def ib_flap_lustre(w: World) -> dict:
    j, node = w.job, w.rng.choice(w.job.nodes)
    ost = w.rng.choice(w.osts)
    oss = f"sim-oss-{int(ost[3:], 16) // 4 + 1:02d}"
    t = j.end - timedelta(minutes=w.rng.randint(8, 20))
    port = w.ports[node]
    flaps = w.rng.randint(4, 9)
    for k in range(flaps):
        down = t + timedelta(seconds=k * w.rng.randint(40, 90))
        w.add(down, "syslog", node, w.p.link_down(), key=True)
        w.add(w.jitter(down, 3, 12), "syslog", node, w.p.link_up())
    w.add(w.jitter(t, 30, 60), "syslog", node,
          w.p.lnet_timeout())
    w.add(w.jitter(t, 60, 120), "syslog", node,
          f"kernel: Lustre: {w.p.filesystem}-{ost}-osc-ffff9a3c1d2e8000: Connection to {w.p.filesystem}-{ost} (at {oss}{w.p.nid_suffix}) "
          "was lost; in progress operations using this service will wait for recovery to complete")
    w.add(j.end - timedelta(seconds=5), "syslog", node,
          f"kernel: LustreError: 11-0: {w.p.filesystem}-{ost}-osc-ffff9a3c1d2e8000: operation ost_write to node "
          f"{oss}{w.p.nid_suffix} failed: rc = -107")
    port.link_downed, port.symbol_errors, port.rcv_errors = flaps, w.rng.randint(800, 4000), w.rng.randint(50, 400)
    j.state, j.exit_code = "FAILED", "5:0"
    job_start(w)
    job_end(w, "exit")
    w.user_report = f"job {j.job_id} failed: 'OSError: [Errno 5] Input/output error' while writing a checkpoint"
    return dict(classification=["network", "multi-domain"], outcome="supported_hypothesis",
                leading=["network.link_flap", "network.link_down"], forbidden=["storage.capacity"])


def ost_full(w: World) -> dict:
    j, node = w.job, w.rng.choice(w.job.nodes)
    ost = w.rng.choice(w.osts)
    w.ost_used[ost] = 100
    pid = w.pid()
    t = j.end - timedelta(seconds=w.rng.randint(2, 10))
    w.add(t, "syslog", node,
          f"kernel: LustreError: {pid}:0:(osc_request.c:1890:osc_brw_fini_request()) {w.p.filesystem}-{ost}-osc-"
          "ffff9a3c1d2e8000: brw_interpret: rc = -28", key=True)
    w.add(w.jitter(t, 0.1, 0.5), "syslog", node,
          f"kernel: LustreError: {pid}:0:(vvp_io.c:1495:vvp_io_commit_write()) Write page 1048576 of inode "
          "ffff9a3c55aa1e00 failed -28")
    j.state, j.exit_code = "FAILED", "1:0"
    job_start(w)
    job_end(w, "exit")
    w.user_report = f"job {j.job_id} failed: 'OSError: [Errno 28] No space left on device'"
    return dict(classification=["storage", "multi-domain"], outcome="supported_hypothesis",
                leading=["storage.capacity"], forbidden=["network.link_flap", "network.link_down"])


def ost_imbalance(w: World) -> dict:
    j, node = w.job, w.rng.choice(w.job.nodes)
    hot = w.rng.sample(w.osts, 2)
    for ost in w.osts:
        w.ost_used[ost] = w.rng.randint(96, 99) if ost in hot else w.rng.randint(35, 55)
    t = j.end - timedelta(seconds=w.rng.randint(2, 10))
    w.add(t, "syslog", node,
          f"kernel: LustreError: {w.pid()}:0:(osc_request.c:1890:osc_brw_fini_request()) {w.p.filesystem}-{hot[0]}-osc-"
          "ffff9a3c1d2e8000: brw_interpret: rc = -28", key=True)
    j.state, j.exit_code = "FAILED", "1:0"
    job_start(w)
    job_end(w, "exit")
    w.user_report = f"job {j.job_id} failed writing output although lfs df shows plenty of free space overall"
    return dict(classification=["storage", "multi-domain"], outcome="supported_hypothesis",
                leading=["storage.ost_imbalance", "storage.capacity"], forbidden=[])


def logs_missing(w: World) -> dict:
    """The job record was purged and the logs around the failure were not kept: nothing to find."""
    w.export_sacct = False
    w.job.state, w.job.exit_code = "FAILED", "1:0"
    # no job lines at all: the window the job ran in is outside the kept logs
    w.user_report = f"job {w.job.job_id} failed last week, no error message in the output"
    return dict(classification=["unknown", "scheduler"], outcome="no_supported_hypothesis",
                leading=[], forbidden=["node.unhealthy", "gpu.hardware", "storage.capacity"],
                required_missing=["get_job_status"])


FAULT_FUNCTIONS: dict[str, Callable[[World], dict]] = {
    "oom_kill": oom_kill, "segfault": segfault, "walltime": walltime, "admin_cancel": admin_cancel,
    "memory_ecc_node_down": memory_ecc_node_down, "gpu_xid79": gpu_xid79, "gpu_xid48": gpu_xid48,
    "gpu_xid13_app": gpu_xid13_app, "ib_flap_lustre": ib_flap_lustre, "ost_full": ost_full,
    "ost_imbalance": ost_imbalance, "logs_missing": logs_missing,
}
