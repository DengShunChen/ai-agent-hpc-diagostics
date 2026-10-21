# Demo guide

About 15 minutes. The demo is offline and uses synthetic data. Nothing it does touches any
real system.

**Message:** *AI proposes. Tools provide evidence. Humans decide.*

## Preparation

```bash
./scripts/run_tests.sh -q
```

```bash
./scripts/demo.sh
```

`scripts/demo.sh` runs every step below against a throwaway state directory. Run it once
beforehand. For a live walkthrough, `DEMO_PAUSE=1 ./scripts/demo.sh` pauses between steps.

For the browser part, keep the token file outside the repository:

```bash
PYTHONPATH=src .venv/bin/python -m hpc_agent.main create-operator-token ops.demo --tokens-file ~/.config/hpc-agent/operators.json
```

```bash
HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json scripts/podman_run.sh
```

Then open http://127.0.0.1:8080/ and paste the token. Use `scripts/run_api.sh` instead of
the container if Podman isn't available. Note that the first container start re-owns the
image layers once, which can take several minutes on a busy host.

## Walkthrough

| # | Show | Point to make |
|---|---|---|
| 1 | Fixture validation and tool allowlist | Agents can reach only nine read-only tools, and every one reads saved synthetic data. |
| 2 | `run_shell`, `execute_command`, `ssh_execute` refused | No shell tool exists under any name, and unknown tools fail closed. |
| 3 | **POC-STORAGE-001** report | "Filesystem utilization is high" is a *claim*. The platform says so, lists the four missing measurements and the checks that would provide them, and **does not** conclude that OST capacity caused the failure. This is the primary acceptance criterion. |
| 4 | `approve` as `rca-coordinator` is refused; a revision request without a comment is refused | No agent can approve. Human decisions need a human identity and a reason. A revision produces a new run linked to the old one. |
| 5 | **POC-SCHEDULER-001** | SIGKILL and the drained node are tied at 0.30, with no false precision. The walltime cause is **ruled out**, citing the contradicting evidence. The timeline places the drain 2 minutes before the failure. |
| 6 | **POC-GPU-001** review, approve, close | XID 13 with an illegal memory access points to the application first. **No hardware conclusion**, and the five missing telemetry fields are listed. The DCGM diagnostic is recommended for an operator, never run. Approval is *recorded*, never executed. |
| 7 | `verify-audit`, then an edited copy | The audit trail is hash-chained, so editing who approved a decision is detected. |
| 8 | Evaluation summary | Metrics over labelled synthetic cases, including a **0% unsupported-claim rate** and **100% correct refusals**. Also the limits (below). |
| 9 | (optional) Browser review page | The seven review fields per proposed action; decisions made with an authenticated token. |
| 10 | (optional) `scripts/container_smoke_test.sh` | The container is rootless and non-root, with a read-only root filesystem, no capabilities, localhost-only access and read-only data. |

## What not to claim

- **No MTTR reduction.** It hasn't been measured, and it can only be reported after
  measurement against representative incidents.
- **No accuracy claim for real incidents.** The evaluation labels are synthetic and were
  written by the developer, so they show designed behavior, not field accuracy.
- **No LLM.** This POC's agents are deterministic and rule-based. The architecture leaves
  room for an LLM to phrase findings later, under the same validation.
- **No production access** of any kind: no FEFS/Lustre, PJM/Slurm, network devices or DCGM.

## Likely questions

- **Why are confidence values so low?** Confidence reflects the evidence actually available.
  The scenarios were built to lack decisive evidence, and refusing to overstate is the point.
- **Who confirms the root cause?** A human. The coordinator ranks hypotheses but never
  confirms one, and a static test enforces that.
- **What would a production pilot need?** A separate security review, read-only integrations
  behind the Tool Gateway, SSO and TLS in front of the API, representative incident data,
  and an independent evaluation.
