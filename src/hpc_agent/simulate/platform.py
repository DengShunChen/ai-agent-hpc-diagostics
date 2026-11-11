"""What differs between simulated platforms: names, scheduler messages, interconnect messages.

slurm-x86   Slurm (slurmctld + slurmd logs), InfiniBand (mlx5), Lustre, x86 CPU nodes, A100 GPU nodes.
tcs-fx1000  Fujitsu TCS style: PJM job manager log, Tofu interconnect, FEFS, FX1000 (A64FX) CPU nodes,
            RX2570 M6 + A100 GPU nodes managed by the same PJM.

The TCS-style messages are **approximations** written for this simulator from general knowledge of
those products; they are not copied from real PJM, Tofu or FEFS output and must be checked against
real (de-identified) logs before a drill on the target cluster.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    name: str
    controller: str
    cpu_prefix: str
    gpu_prefix: str
    filesystem: str
    nid_suffix: str
    scheduler_source: str  # log source of the job manager
    node_source: str | None  # per-node scheduler daemon log, if the platform exports one
    cpus_per_node: int
    cpu_partition: str
    gpu_partition: str

    # --- job manager messages -----------------------------------------------------------------
    def allocate(self, job_id: int, nodelist: str, nodes: int, partition: str) -> str:
        if self.name == "tcs-fx1000":
            return f"JOB {job_id} STARTED: rscgrp={partition} node={nodes} nodelist={nodelist}"
        return f"sched: Allocate JobId={job_id} NodeList={nodelist} #CPUs={self.cpus_per_node * nodes} Partition={partition}"

    def complete(self, job_id: int, how: str, code: str, sig: str) -> list[str]:
        if self.name == "tcs-fx1000":
            return [f"JOB {job_id} ENDED: end_code={code} signal={sig if how == 'signal' else 0}"]
        detail = f"WTERMSIG {sig}" if how == "signal" else f"WEXITSTATUS {code}"
        return [f"_job_complete: JobId={job_id} {detail}", f"_job_complete: JobId={job_id} done"]

    def time_limit(self, job_id: int) -> str:
        if self.name == "tcs-fx1000":
            return f"JOB {job_id} KILLED: elapse limit exceeded"
        return f"Time limit exhausted for JobId={job_id}"

    def admin_delete(self, job_id: int) -> str:
        if self.name == "tcs-fx1000":
            return f"JOB {job_id} DELETED: requested by uid 0 (pjdel)"
        return f"_slurm_rpc_kill_job: REQUEST_KILL_JOB JobId={job_id} uid 0"

    def memory_limit(self, job_id: int, node: str) -> str | None:
        """The job manager's own record of a memory-limit kill (TCS); Slurm logs it on the node instead."""
        if self.name == "tcs-fx1000":
            return f"JOB {job_id} KILLED: memory limit exceeded on {node}"
        return None

    def node_reason(self, node: str, reason: str) -> list[str]:
        if self.name == "tcs-fx1000":
            return [f"NODE {node} STATE CHANGED: ACTIVE -> STOP (reason: {reason})"]
        return [f"update_node: node {node} reason set to: {reason}", f"update_node: node {node} state set to DRAINING"]

    def node_down(self, node: str, job_id: int) -> list[str]:
        if self.name == "tcs-fx1000":
            return [f"NODE {node} STATE CHANGED: STOP -> DOWN (reason: no response)",
                    f"JOB {job_id} KILLED: node {node} down"]
        return [f"error: Nodes {node} not responding, setting DOWN", f"Killing JobId={job_id} on failed node {node}"]

    def maintenance(self, node: str, reason: str) -> list[str]:
        if self.name == "tcs-fx1000":
            return [f"NODE {node} STATE CHANGED: ACTIVE -> STOP (reason: {reason})"]
        return [f"update_node: node {node} reason set to: {reason}", f"update_node: node {node} state set to DRAINED"]

    def housekeeping(self, tested: int) -> list[str]:
        if self.name == "tcs-fx1000":
            return [f"SCHEDULE: cycle completed, {tested} jobs examined"]
        return ["backfill: beginning", f"backfill: reached end of job queue (tested {tested} jobs)"]

    # --- interconnect ---------------------------------------------------------------------------
    def link_down(self) -> str:
        if self.name == "tcs-fx1000":
            return "kernel: tofu: TNI2 port C: link down"
        return "kernel: mlx5_core 0000:5e:00.0: mlx5_port_event: port 1 link down"

    def link_up(self) -> str:
        if self.name == "tcs-fx1000":
            return "kernel: tofu: TNI2 port C: link up (retrained)"
        return "kernel: mlx5_core 0000:5e:00.0: mlx5_port_event: port 1 link up"

    def lnet_timeout(self) -> str:
        if self.name == "tcs-fx1000":
            return "kernel: LNetError: 3511:0:(tofulnd_cb.c:1402:ktofu_check_txs()) Timed out tx: tx_queue, 6 seconds"
        return "kernel: LNetError: 3511:0:(o2iblnd_cb.c:3402:kiblnd_check_txs_locked()) Timed out tx: tx_queue, 6 seconds"

    # --- memory error (generic Linux APEI on A64FX; EDAC on x86) -----------------------------------
    def memory_ue(self) -> list[str]:
        if self.name == "tcs-fx1000":
            return ["kernel: {1}[Hardware Error]: Hardware error from APEI Generic Hardware Error Source: 2",
                    "kernel: {1}[Hardware Error]: event severity: fatal",
                    "kernel: {1}[Hardware Error]:  section_type: memory error"]
        return ["kernel: mce: [Hardware Error]: Machine check events logged",
                "kernel: EDAC MC1: 1 UE memory read error on CPU_SrcID#1_MC#0_Chan#2_DIMM#0 (channel:2 slot:0 "
                "page:0x8a3f12 offset:0x0 grain:32 - err_code:0x0000:0x009f socket:1 imc:0 rank:0 bg:1 ba:2 "
                "row:0x1a3f col:0x3c0)"]

    def memory_ce(self) -> str:
        if self.name == "tcs-fx1000":
            return "kernel: {2}[Hardware Error]: event severity: corrected, section_type: memory error"
        return ("kernel: EDAC MC0: 1 CE memory read error on CPU_SrcID#0_MC#0_Chan#1_DIMM#0 (channel:1 slot:0 "
                "page:0x3b21a offset:0x0 grain:32 syndrome:0x0)")


SLURM_X86 = Platform(name="slurm-x86", controller="sim-ctl-01", cpu_prefix="sim-node", gpu_prefix="sim-gpu-node",
                     filesystem="simfs", nid_suffix="@o2ib", scheduler_source="slurmctld", node_source="slurmd",
                     cpus_per_node=64, cpu_partition="batch", gpu_partition="gpu")
TCS_FX1000 = Platform(name="tcs-fx1000", controller="sim-pjm-01", cpu_prefix="sim-fx", gpu_prefix="sim-gx",
                      filesystem="simfefs", nid_suffix="@tofu", scheduler_source="pjm", node_source=None,
                      cpus_per_node=48, cpu_partition="small", gpu_partition="gpu-a100")
PLATFORMS = {p.name: p for p in (SLURM_X86, TCS_FX1000)}
