# HPC/AI Cluster Diagnostics POC

**Human-in-the-Loop Multi-AI Agent Diagnostics Platform for HPC/AI Clusters**
基於 Multi-AI Agent 與人工核准機制之 HPC/AI 叢集智慧故障診斷平台

> AI proposes. Tools provide evidence. Humans decide.

## Purpose

This platform helps engineers investigate incidents across HPC storage (FEFS/Lustre), job
schedulers (PJM/Slurm), network infrastructure and GPU infrastructure. Specialized AI
agents gather evidence through a controlled Tool Gateway and propose a root-cause
assessment. A human then reviews it and decides what happens next.

It is **not** an autonomous remediation platform.

## POC scope

- Offline proof of concept on a single Rocky Linux 10 server.
- Synthetic, de-identified data only (`fixtures/`, `test_data/`, `knowledge/`).
- Production HPC, storage, scheduler, network and GPU access is **disabled**.
- No SSH automation, no sudo for agents, no shell tools, no automatic remediation.

**Current status: Iteration 1 is complete.** It provides the models, fixtures, Tool Gateway,
safety policy and tests. Agents, LLM calls, the API and the container come in later iterations.

## Architecture

```
Incident/Alert -> Web UI/API -> Triage Agent -> {Storage, Scheduler, Network, GPU} Agents
              -> RCA Coordinator -> RCA Assessment -> Recommendations -> Human Approval
```

All operational data enters through the read-only data adapter and the allowlisted Tool
Gateway. See [docs/architecture.md](docs/architecture.md).

## Safety principles

- The safety policy denies anything not explicitly allowed; unknown operations are denied.
- The Tool Gateway uses an explicit registry. Unknown tools fail closed, and shell, exec or
  ssh-style tools cannot be registered under any name.
- Data reads are confined to `fixtures/`, `test_data/` and `knowledge/`. Each read is
  size-limited and scanned for credentials.
- The models refuse unsupported conclusions. A finding needs evidence, and confidence
  without evidence is rejected. A confirmed root cause needs tool-derived evidence and no
  conflicting evidence.
- Approval, rejection and revision decisions need a human operator identity. APPROVED
  executes nothing.

See [docs/safety.md](docs/safety.md).

## Agent responsibilities (planned, Iterations 2–3)

| Agent | Responsibility |
|---|---|
| Triage | Extract symptoms, identify available/missing evidence, classify domain, select specialists |
| Storage | FEFS/Lustre: capacity, OST utilization/imbalance, I/O errors, health |
| Scheduler | PJM/Slurm: job/node/queue state, allocation, abnormal termination |
| Network | NIC/bond/Ethernet status, interface errors, saved SNMP/monitoring data |
| GPU | Saved DCGM-style health, XID/CUDA errors, memory, PCIe, NVLink, power, thermal |
| RCA Coordinator | Correlate findings, rank hypotheses, separate fact / hypothesis / recommendation / missing info |

## Workflow and approval states

`DRAFT → DIAGNOSIS_COMPLETE → AWAITING_HUMAN_APPROVAL → APPROVED → CLOSED`
plus `REJECTED` and `REVISION_REQUESTED`, which both lead to `REVISE` and then back to `DRAFT`.

## Repository structure

```
config/            agents.yaml, app.yaml (documented settings; not loaded yet)
src/hpc_agent/
  models/          Incident, AgentDiagnosis, RCAResult, AuditRecord, ApprovalRecord
  safety/          policy.py (default deny), approval.py (state machine), secrets.py
  tools/           gateway.py (allowlist) + per-domain read-only tools
  data_adapter.py  read-only access to fixtures/, test_data/, knowledge/
  agents/ workflows/ api/ logging/   placeholders for later iterations
fixtures/incidents/   synthetic POC incidents
test_data/<domain>/<incident_id>/   saved synthetic tool outputs
knowledge/{incidents,sop,manuals}/  knowledge base (empty in Iteration 1)
tests/{unit,safety,scenarios}/
containers/Containerfile  placeholder (Iteration 6)
docs/              architecture.md, safety.md, scenarios.md
```

## Requirements

- Rocky Linux 10 (developed on 10.2), Python 3.11+ (uses the system `/usr/bin/python3.12`)
- Project-local packages: `pydantic>=2.7,<3`, `pytest>=8,<9`
- Podman 5.x (for Iteration 6 only)
- Microsoft Agent Framework: not needed yet (Iteration 2+)

## Setup

```bash
/usr/bin/python3.12 -m venv .venv
```

```bash
.venv/bin/python -m pip install "pydantic>=2.7,<3" "pytest>=8,<9"
```

## Configuration

Copy `.env.example` to `.env` and fill it in locally. **Never commit `.env`.** Iteration 1 needs
no configuration. `HPC_AGENT_DATA_ROOT` can point the data adapter at another data root.

## Running tests

```bash
./scripts/run_tests.sh
```

## Running scenarios

Iteration 1 validates the scenario fixtures and lists the allowlisted tools:

```bash
./scripts/run_dev.sh
```

End-to-end scenario runs start in Iteration 2. See [docs/scenarios.md](docs/scenarios.md).

## Future Podman deployment (Iteration 6)

The plan is one rootless, non-root container. `knowledge/` and `test_data/` will be mounted
read-only, and `audit-data/` will be the only writable volume. The image will contain no
credentials or SSH keys, and the container will run without privileged mode or host
networking.

## Known limitations

- No agents, LLM integration, workflow, API or UI yet.
- Operator identity is a naming-rule guard only; real authentication is future work.
- The credential scanner is pattern-based and can miss novel secret formats.
- Audit records are modelled but not yet persisted (Iteration 5).
- No MTTR claims are made. MTTR reduction may only be reported after measurement against
  representative incidents.

## Roadmap

1. ✅ Environment, models, fixtures, safety foundation, tests
2. Triage, Storage, Scheduler agents (first end-to-end run on POC-STORAGE-001)
3. Network and GPU agents, RCA Coordinator
4. Full Tool Gateway, knowledge tools, evidence correlation
5. Human-in-the-loop workflow, append-only audit logging
6. API, optional simple UI, Containerfile, Podman deployment
7. Evaluation, covering domain classification, root-cause accuracy, evidence quality,
   unsupported-claim rate, human override rate and processing time
