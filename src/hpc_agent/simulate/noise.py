"""Background log volume and misleading signals.

Background: other users' jobs starting and ending (some of them failing, as on any real cluster),
scheduler housekeeping, routine daemon messages and harmless kernel chatter. Distractors are
signals that look relevant but are not the injected cause.
"""

from __future__ import annotations

from datetime import timedelta

from hpc_agent.simulate.catalog import hostlist, node_log, sched
from hpc_agent.simulate.world import World

ROUTINE = (
    "systemd[1]: Starting dnf makecache...",
    "systemd[1]: dnf-makecache.service: Deactivated successfully.",
    "systemd[1]: Finished dnf makecache.",
    "chronyd[1123]: Selected source sim-ntp-01",
    "chronyd[1123]: System clock wrong by 0.000412 seconds",
    "rsyslogd[1051]: imjournal: journal files changed, reloading...",
    "systemd[1]: logrotate.service: Deactivated successfully.",
    "systemd[1]: Started Hourly session cleanup.",
    "kernel: perf: interrupt took too long (2510 > 2500), lowering kernel.perf_event_max_sample_rate to 79500",
    "smartd[1210]: Device: /dev/nvme0, SMART Health Status: OK",
    "nhc[2231]: Node Health Check completed successfully (0s)",
    "systemd[1]: Started Slurm node daemon health probe.",
    "munged[1301]: Purged 0 credentials from replay hash",
    "auditd[981]: Audit daemon rotating log files",
)


def background(w: World) -> None:
    rng, sc = w.rng, w.sc
    window = sc.hours * 3600
    # Other jobs: about 12 per hour, scaled by noise_level.
    for _ in range(int(sc.hours * 12 * sc.noise_level)):
        size = rng.randint(1, 3)
        pool = w.gpu_nodes if rng.random() < 0.3 else w.cpu_nodes
        nodes = sorted(rng.sample(pool, min(size, len(pool))))
        start = sc.start + timedelta(seconds=rng.uniform(0, window))
        end = start + timedelta(minutes=rng.randint(3, 240))
        job_id = rng.randint(7_000_000, 7_999_999)
        if job_id == w.job.job_id:
            continue
        gpu_job = pool is w.gpu_nodes
        sched(w, start, w.p.allocate(job_id, hostlist(nodes), len(nodes),
                                     w.p.gpu_partition if gpu_job else w.p.cpu_partition))
        for i, node in enumerate(nodes):
            if i == 0:
                node_log(w, w.jitter(start, 0.2, 1.5), node, f"Launching batch job {job_id} for UID "
                         f"{rng.randint(20002, 20090)}")
            node_log(w, w.jitter(start, 5, 20), node, f"launch task StepId={job_id}.0 request from UID:"
                     f"{rng.randint(20002, 20090)} GID:20000")
        outcome = rng.random()
        if outcome < 0.78:
            how, code, sig = "exit", "0", "0"
        elif outcome < 0.93:
            how, code, sig = "exit", str(rng.choice((1, 1, 2, 127))), "0"  # ordinary application failures
        else:
            how, code, sig = "signal", "0", "15"
            sched(w, end - timedelta(milliseconds=300), w.p.time_limit(job_id))
        for k, text in enumerate(w.p.complete(job_id, how, code, sig)):
            sched(w, end if k == 0 else w.jitter(end, 0.001, 0.01), text)
        for node in nodes:
            node_log(w, w.jitter(end, 0.5, 3), node, f"[{job_id}.0] done with job")
    # Scheduler housekeeping every few minutes.
    t = sc.start
    while t < w.end:
        second = w.jitter(t, 0.05, 0.4)  # drawn before the job count, so a seed gives the same lines as before
        for k, text in enumerate(w.p.housekeeping(rng.randint(3, 60))):
            sched(w, t if k == 0 else second, text)
        t += timedelta(seconds=rng.randint(120, 300) / max(sc.noise_level, 0.1))
    # Routine syslog on every node: about ten messages per node per hour.
    for node in w.nodes:
        for _ in range(int(sc.hours * 10 * sc.noise_level)):
            w.add(sc.start + timedelta(seconds=rng.uniform(0, window)), "syslog", node, rng.choice(ROUTINE))


def distractors(w: World) -> None:
    rng, j = w.rng, w.job
    others = [n for n in w.nodes if n not in j.nodes]
    for kind in w.sc.distractors:
        if kind == "other_node_xid13" and others:
            node = rng.choice([n for n in others if n in w.gpu_nodes] or others)
            pci = w.gpus[node][0].pci if node in w.gpus else "0000:3b:00"
            w.add(w.jitter(j.end, -300, 300), "syslog", node,
                  f"kernel: NVRM: Xid (PCI:{pci}): 13, pid={w.pid()}, name=python3, Graphics SM Warp Exception on "
                  "(GPC 1, TPC 0, SM 1): Out Of Range Address")
        elif kind == "old_correctable_ecc":
            node = rng.choice(j.nodes)
            when = w.sc.start + timedelta(minutes=rng.randint(5, 40))
            for k in range(rng.randint(2, 5)):
                w.add(when + timedelta(seconds=k * 7), "syslog", node, w.p.memory_ce())
        elif kind == "recovered_link_flap":
            node = rng.choice(j.nodes)
            when = w.sc.start + timedelta(minutes=rng.randint(10, 60))
            w.add(when, "syslog", node, w.p.link_down())
            w.add(w.jitter(when, 4, 9), "syslog", node, w.p.link_up())
            w.ports[node].link_downed = max(w.ports[node].link_downed, 1)
        elif kind == "other_job_oom" and others:
            node, pid = rng.choice(others), w.pid()
            other = rng.randint(7_000_000, 7_999_999)
            when = w.jitter(j.end, -1800, 600)
            w.add(when, "syslog", node, f"kernel: Memory cgroup out of memory: Killed process {pid} (python3) "
                  f"total-vm:98012344kB, anon-rss:95981220kB, file-rss:8240kB, shmem-rss:0kB, UID:20044 "
                  "pgtables:202312kB oom_score_adj:0")
            if w.p.memory_limit(other, node):
                sched(w, w.jitter(when, 1, 2), w.p.memory_limit(other, node))
            node_log(w, w.jitter(when, 1, 2), node, f"[{other}.0] error: Detected 1 oom_kill event in StepId="
                  f"{other}.0. Some of the step tasks have been OOM Killed.")
        elif kind == "lustre_reconnects":
            for _ in range(rng.randint(6, 15)):
                node = rng.choice(w.nodes)
                ost = rng.choice(w.osts)
                when = w.sc.start + timedelta(seconds=rng.uniform(0, w.sc.hours * 3600))
                w.add(when, "syslog", node, f"kernel: Lustre: {w.p.filesystem}-{ost}-osc-ffff9a3c1d2e8000: Connection "
                      f"restored to {w.p.filesystem}-{ost} (at sim-oss-{int(ost[3:], 16) // 4 + 1:02d}{w.p.nid_suffix})")
        elif kind == "drained_other_node" and others:
            node = rng.choice(others)
            when = w.sc.start + timedelta(minutes=rng.randint(5, 90))
            reason = "maintenance: BIOS update scheduled"
            lines = w.p.maintenance(node, reason)
            sched(w, when, lines[0])
            if len(lines) > 1:
                sched(w, w.jitter(when, 0.01, 0.1), lines[1])
            w.node_state[node] = ("DRAINED", reason, when)
