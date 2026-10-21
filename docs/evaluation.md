# POC Evaluation Report

Generated 2026-10-21 09:02 UTC by `hpc-agent evaluate` over 13 synthetic cases.

> **Read this first.** All cases are synthetic, and their labels were written by the developer
> of the agents. The numbers show that the system behaves as designed on designed examples;
> they are **not** evidence of accuracy on real incidents. The agents are deterministic, so
> processing times say nothing about LLM latency. **No MTTR reduction is claimed.**

## Summary

| Metric | Result |
|---|---|
| Correct domain classification | 85% |
| Root cause accuracy, leading hypothesis (top-1) | 100% |
| Root cause accuracy, top-3 | 100% |
| Outcome matches label (supported / insufficient / none) | 100% |
| Correct refusal to conclude (cases labelled insufficient or none) | 100% |
| Evidence quality: open hypotheses backed by tool evidence | 94% |
| Evidence quality: required missing information identified | 100% |
| Unsupported claim rate (per open hypothesis) | 0% (0 claims) |
| Human override rate | not measured: no recorded human decisions were supplied |
| Diagnostic processing time (mean / median / max) | 7 / 7 / 8 ms |

## Per-case results

| Case | Classification | Outcome (expected) | Leading hypothesis | Conf. | Top-1 | Tool-backed | Unsupported | Missing found | ms |
|---|---|---|---|---|---|---|---|---|---|
| EVAL-GPU-001 | gpu | supported_hypothesis | gpu.hardware | 0.50 | yes | 1/1 | 0 | 0/0 | 8 |
| EVAL-GPU-002 | gpu | supported_hypothesis | gpu.thermal | 0.40 | yes | 1/1 | 0 | 0/0 | 6 |
| EVAL-NET-001 | multi-domain | supported_hypothesis | network.link_down | 0.50 | yes | 2/2 | 0 | 1/1 | 7 |
| EVAL-SCHED-001 | scheduler | supported_hypothesis | scheduler.walltime | 0.50 | yes | 2/2 | 0 | 0/0 | 6 |
| EVAL-SCHED-002 | scheduler | supported_hypothesis | application.crash | 0.30 | yes | 1/1 | 0 | 0/0 | 6 |
| EVAL-STOR-001 | **multi-domain** | supported_hypothesis | storage.capacity | 0.60 | yes | 2/2 | 0 | 1/1 | 7 |
| EVAL-STOR-002 | storage | no_supported_hypothesis | – | 0.00 | – | 0/0 | 0 | 0/0 | 8 |
| EVAL-STOR-003 | **multi-domain** | supported_hypothesis | storage.ost_imbalance | 0.40 | yes | 1/1 | 0 | 0/0 | 7 |
| EVAL-TIME-001 | scheduler | supported_hypothesis | scheduler.signal_termination | 0.30 | yes | 2/2 | 0 | 0/0 | 6 |
| EVAL-UNK-001 | unknown | no_supported_hypothesis | – | 0.00 | – | 0/0 | 0 | 1/1 | 1 |
| POC-GPU-001 | gpu | supported_hypothesis | gpu.application | 0.35 | yes | 1/1 | 0 | 1/1 | 7 |
| POC-SCHEDULER-001 | scheduler | supported_hypothesis | scheduler.signal_termination | 0.30 | yes | 2/2 | 0 | 2/2 | 8 |
| POC-STORAGE-001 | storage | insufficient_evidence | storage.capacity | 0.25 | yes | 0/1 | 0 | 4/4 | 7 |

## Scenario comparison (POC scenarios)

| | POC-GPU-001 | POC-SCHEDULER-001 | POC-STORAGE-001 |
|---|---|---|---|
| Classification | gpu | scheduler | storage |
| Outcome | supported hypothesis | supported hypothesis | insufficient evidence |
| Leading hypothesis | Application or CUDA software-environment fault on sim-gpu-node-0007 GPU 3: XID 13 (graphics engine exception) is commonly associated with application errors rather than GPU hardware | Job processes were killed (SIGKILL); possible causes include an out-of-memory kill, a scheduler or administrator kill, or a node failure | Storage capacity or OST imbalance may be contributing to the write failure |
| Leading confidence | 0.35 | 0.30 | 0.25 |
| Open hypotheses (tool-backed) | 1 (1) | 2 (2) | 1 (0) |
| Facts: reported / tool | 3 / 2 | 5 / 5 | 2 / 0 |
| Knowledge references | 3 | 4 | 4 |
| Proposed actions | 0 | 0 | 0 |
| Root cause confirmed | no | no | no |

## Unsupported claims

None detected.

## Mismatches against labels

- EVAL-STOR-001: classification=multi-domain, outcome=supported_hypothesis (expected supported_hypothesis), leading=storage.capacity
- EVAL-STOR-003: classification=multi-domain, outcome=supported_hypothesis (expected supported_hypothesis), leading=storage.ost_imbalance

## Method

- Cases live in `evaluation/cases/`. Each case runs through the full workflow (triage,
  specialists, RCA Coordinator, runtime guard) in an isolated temporary data root.
- **Outcome** is `supported_hypothesis` when the leading open hypothesis has tool evidence,
  `insufficient_evidence` when it rests only on reported symptoms, and
  `no_supported_hypothesis` when nothing is open.
- **Unsupported claims** are confirmed causes, open hypotheses with confidence of at least
  0.5 and no tool evidence, and hypotheses in a case's forbidden categories.
- **Human override rate** counts REJECTED and REVISION_REQUESTED among recorded human
  decisions (`--approvals-log`). It is never simulated.
- **Next step toward a real evaluation:** representative, de-identified incidents labelled
  independently by operations engineers, plus override rates from real reviews.
