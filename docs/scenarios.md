# POC Scenarios

All scenario data is synthetic. Each scenario deliberately contains too little evidence for
an easy diagnosis. **The ability to refuse an unsupported conclusion is a primary acceptance
criterion.**

## Scenario 01: POC-STORAGE-001 (FEFS/Lustre write failure)

- **Fixture**: `fixtures/incidents/POC-STORAGE-001.json`. The symptoms are "job failed while
  writing output" and "filesystem utilization is high". There are no alerts, logs or notes.
- **Saved tool data**: none. `get_filesystem_usage`, `get_ost_usage` and `get_job_status`
  all return `no_data`.
- **Must NOT conclude**: "OST capacity caused the failure." The models reject a confirmed
  cause that rests only on reported symptoms.
- **Expected output**:
  - HYPOTHESIS: storage capacity or OST imbalance may be contributing to the write failure.
  - EVIDENCE: the job failed while writing output, and filesystem utilization was reported as high.
  - MISSING INFORMATION: the actual filesystem utilization percentage, per-OST utilization,
    filesystem health status and job termination details.
  - RECOMMENDED CHECKS: `get_filesystem_usage()`, `get_ost_usage()`, `get_job_status()`.

## Scenario 02: POC-SCHEDULER-001 (scheduler job failure)

- **Fixture**: job `SIM-JOB-20001` ended early with exit code 137, and node `sim-node-0042`
  changed to DRAIN. The operator has not yet checked the drain reason.
- **Saved tool data**: `get_job_status` shows the job FAILED with `termination_reason: null`.
  `get_node_status` shows `sim-node-0042` as DRAIN with a null reason and `sim-node-0043`
  as ALLOCATED. Network, storage and GPU data are absent.
- **Expected behavior**: classify the incident as scheduler-related but possibly
  multi-domain. Exit code 137 means SIGKILL, which is consistent with an OOM kill, a
  scheduler or admin kill, or a node problem, but proves none of them. The drain does not
  explain itself. A failed job does **not** by itself mean the scheduler failed. The
  missing information is the drain reason, the job termination reason, node memory and OOM
  events, and storage, network and GPU state at the time.

## Scenario 03: POC-GPU-001 (GPU node error)

- **Fixture**: a training job aborted with "CUDA error: an illegal memory access was
  encountered", and monitoring reported an XID event on `sim-gpu-node-0007` GPU 3.
- **Saved tool data** (DCGM-style and synthetic; **no real DCGM is run**): GPU 3 health is
  `warn` with one XID 13 event. Temperature, power, ECC, PCIe replay and NVLink fields are
  all `null`. GPUs 0–2 pass.
- **Expected behavior**: do **not** conclude a hardware failure. An illegal memory access
  together with XID 13 is often caused by the application or the CUDA software
  environment. The categories to keep open are GPU health, memory/ECC, PCIe, NVLink,
  power, thermal and the CUDA/software environment. The missing information is ECC
  counters, thermal and power history, PCIe and NVLink counters, driver and CUDA
  versions, whether the error reproduces on other GPUs or nodes, and the application
  code path.

## Status (Iteration 3)

You can run each scenario with `./scripts/run_dev.sh diagnose <ID>`. The tests are in
`tests/scenarios/test_scenario_workflows.py`.

| Scenario | RCA classification | Agents run | Result |
|---|---|---|---|
| POC-STORAGE-001 | storage | storage, scheduler | One hypothesis (capacity or OST imbalance) at confidence 0.25, with no actions. It reports the four required missing items and the three required checks. The cross-domain relationship is that the "job failed while writing output" symptom concerns both storage and the scheduler. |
| POC-SCHEDULER-001 | scheduler | scheduler | SIGKILL and the drained allocated node are tied at confidence 0.30. The walltime limit is **ruled out**, citing the conflicting evidence. Because the node is implicated and the storage, network and GPU domains weren't examined, checks for those domains are recommended. |
| POC-GPU-001 | gpu | gpu, scheduler | The leading hypothesis is an application or CUDA software fault (XID 13 is commonly associated with application errors), at confidence 0.35. **No hardware conclusion is made.** The five missing hardware telemetry fields are listed, and an operator-run DCGM diagnostic is recommended but not executed. |

Every RCA reports "Root cause NOT confirmed; human review required". A test with synthetic
data (`test_multi_domain_correlation_on_shared_node`) also checks that a node implicated by
both scheduler and network data is classified as `multi-domain` and correlated.
