---
name: xid-triage
description: "Classifying NVIDIA XID events and GPU telemetry into application, hardware or follow-on causes."
agents: [gpu-agent]
---
# What does a GPU's XID event point to?

Cite the GPU (node and index), the XID value and its count from the fetched health data.

## Common XID codes
| XID | Meaning | Usually points to | Category |
|---|---|---|---|
| 13 | graphics engine exception | the application | `gpu.application` |
| 31 | GPU memory page fault | the application | `gpu.application` |
| 43 | GPU stopped processing | the application | `gpu.application` |
| 45 | preemptive cleanup after a prior error | follow-on of another error | (look for the first error) |
| 48 | double-bit ECC error | hardware | `gpu.hardware` |
| 63, 64 | ECC page retirement / row remapping event or failure | hardware | `gpu.hardware` |
| 74 | NVLink error | hardware / interconnect | `gpu.hardware` or `gpu.interconnect` |
| 79 | GPU has fallen off the bus | hardware | `gpu.hardware` |
| 92, 94, 95 | high single-bit ECC rate, contained / uncontained ECC error | hardware | `gpu.hardware` |

- An XID not in this table: say its interpretation is missing; use `gpu.unexplained` only with
  cited data.
- An application-class XID together with a CUDA error in the job log (for example "illegal
  memory access") makes an application fault more likely than a hardware fault.

## Telemetry
- `temperature_c` at or above the site's thermal limit supports `gpu.thermal`.
- `ecc_dbe_volatile` above 0 supports a hardware cause.
- Non-zero `pcie_replay_count` or `nvlink_errors` may indicate interconnect trouble
  (`gpu.interconnect`); significance depends on the site's baseline, so say so.
- Telemetry fields that are null were not recorded: list them as missing, do not assume zero.
