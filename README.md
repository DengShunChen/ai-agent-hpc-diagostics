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

**Current status: Iteration 3 is complete.** All five specialist roles and the RCA Coordinator
run end to end through a Microsoft Agent Framework workflow on synthetic data. The workflow
produces a ranked, evidence-linked RCA that never confirms a root cause on its own. The agents
are deterministic and evidence-grounded, and no LLM is called yet. Knowledge tools, approvals,
audit persistence, the API and the container come in later iterations.

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

## Agent responsibilities

| Agent | Status | Responsibility |
|---|---|---|
| Triage | ✅ Iteration 2 | Extract symptoms, identify available/missing evidence, classify domain, select specialists |
| Storage | ✅ Iteration 2 | FEFS/Lustre: capacity, OST utilization/imbalance, I/O errors, health |
| Scheduler | ✅ Iteration 2 | PJM/Slurm: job/node state, exit signals, walltime, abnormal termination |
| Network | ✅ Iteration 3 | NIC/bond/Ethernet status, interface errors, saved SNMP/monitoring data |
| GPU | ✅ Iteration 3 | Saved DCGM-style health, XID/CUDA errors, memory, PCIe, NVLink, power, thermal |
| RCA Coordinator | ✅ Iteration 3 | Correlate findings, rank hypotheses, record ruled-out causes and conflicting evidence, relate domains; never confirms |

## Workflow and approval states

`DRAFT → DIAGNOSIS_COMPLETE → AWAITING_HUMAN_APPROVAL → APPROVED → CLOSED`
plus `REJECTED` and `REVISION_REQUESTED`, which both lead to `REVISE` and then back to `DRAFT`.

## Repository structure

```
config/            agents.yaml, app.yaml (documented settings; not loaded yet)
src/hpc_agent/
  models/          Incident, AgentDiagnosis, RCAResult, AuditRecord, ApprovalRecord,
                   TriageResult, DiagnosticRun
  agents/          triage.py, storage.py, scheduler.py, evidence.py (network/gpu/coordinator: placeholders)
  workflows/       diagnostic_workflow.py (Agent Framework orchestration + report)
  safety/          policy.py (default deny), approval.py (state machine), secrets.py
  tools/           gateway.py (allowlist, per-agent ScopedGateway) + per-domain read-only tools
  config.py        loads per-agent tool scopes from config/agents.yaml
  data_adapter.py  read-only access to fixtures/, test_data/, knowledge/
  api/ logging/    placeholders for later iterations
fixtures/incidents/   synthetic POC incidents
test_data/<domain>/<incident_id>/   saved synthetic tool outputs
knowledge/{incidents,sop,manuals}/  knowledge base (empty in Iteration 1)
tests/{unit,safety,scenarios}/
containers/Containerfile  placeholder (Iteration 6)
docs/              architecture.md, safety.md, scenarios.md
```

## Requirements

- Rocky Linux 10 (developed on 10.2), Python 3.11+ (uses the system `/usr/bin/python3.12`)
- Project-local packages: `pydantic>=2.7,<3`, `pyyaml>=6,<7`, `agent-framework-core==1.22.0`
  (Microsoft Agent Framework core only, with no cloud connectors), and `pytest>=8,<9` for tests
- Podman 5.x (for Iteration 6 only)

## Setup

```bash
/usr/bin/python3.12 -m venv .venv
```

```bash
.venv/bin/python -m pip install "pydantic>=2.7,<3" "pyyaml>=6,<7" "agent-framework-core==1.22.0" "pytest>=8,<9"
```

## Configuration

Copy `.env.example` to `.env` and fill it in locally. **Never commit `.env`.** Iteration 1 needs
no configuration. `HPC_AGENT_DATA_ROOT` can point the data adapter at another data root.
`config/agents.yaml` sets which gateway tools each agent may call. It can only narrow the
gateway allowlist, never widen it.

## Running tests

```bash
./scripts/run_tests.sh
```

## Running scenarios

Run a diagnosis on a fixture incident. The report keeps facts, hypotheses, missing
information, checks and proposed actions in separate sections:

```bash
./scripts/run_dev.sh diagnose POC-STORAGE-001
```

Running `./scripts/run_dev.sh` with no arguments validates every fixture and lists the
allowlisted tools. See [docs/scenarios.md](docs/scenarios.md).

## Future Podman deployment (Iteration 6)

The plan is one rootless, non-root container. `knowledge/` and `test_data/` will be mounted
read-only, and `audit-data/` will be the only writable volume. The image will contain no
credentials or SSH keys, and the container will run without privileged mode or host
networking.

## Known limitations

- Agents are deterministic rule-based analyzers. No LLM is integrated yet, and that needs a
  configured provider and credentials supplied by a human.
- The coordinator never confirms a root cause. Confirmation is left to the human reviewer
  until a confirmation rule is agreed and validated against representative incidents.
- Cross-domain correlation is rule-based. It links findings through shared incident items
  and shared components (node, job, OST, GPU, interface), not through timing. Timestamps are
  not yet correlated.
- The XID table covers a subset of NVIDIA XID codes. Unknown codes are reported as missing
  interpretation.
- Triage uses keyword signals, so unusual wording can be misclassified as `unknown`. That
  outcome is safe, but not useful.
- No API or UI yet.
- Operator identity is a naming-rule guard only; real authentication is future work.
- The credential scanner is pattern-based and can miss novel secret formats.
- Audit records are modelled but not yet persisted (Iteration 5).
- No MTTR claims are made. MTTR reduction may only be reported after measurement against
  representative incidents.

## Roadmap

1. ✅ Environment, models, fixtures, safety foundation, tests
2. ✅ Triage, Storage, Scheduler agents; Agent Framework workflow; end-to-end scenario runs
3. ✅ Network and GPU agents, RCA Coordinator, cross-domain correlation
4. Full Tool Gateway, knowledge tools, evidence correlation
5. Human-in-the-loop workflow, append-only audit logging
6. API, optional simple UI, Containerfile, Podman deployment
7. Evaluation, covering domain classification, root-cause accuracy, evidence quality,
   unsupported-claim rate, human override rate and processing time
