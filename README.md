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

**Current status: Iteration 6 is complete.** All five specialist roles and the RCA Coordinator
run end to end through a Microsoft Agent Framework workflow on synthetic data. The workflow
produces a ranked, evidence-linked RCA with a timeline, and it never confirms a root cause on
its own.

- Agents cite SOPs, manuals and past incidents from the knowledge base as references only.
- Every run is checked against runtime safety invariants before it is returned.
- The agents are deterministic, and no LLM is called yet.
- Every saved run waits for a human decision: approve, reject or request revision. Decisions
  are recorded in a tamper-evident audit trail and are never executed.
- An authenticated HTTP API and a minimal review page serve the same workflow. Operators
  authenticate with bearer tokens, and only token hashes are stored.
- The app ships as one rootless, non-root Podman container with a read-only root filesystem
  and read-only data volumes; `audit-data/` is its only writable volume.

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
knowledge/{incidents,sop,manuals}/  synthetic knowledge base (Markdown; reference material only)
tests/{unit,safety,scenarios}/
containers/Containerfile  placeholder (Iteration 6)
docs/              architecture.md, safety.md, scenarios.md
```

## Requirements

- Rocky Linux 10 (developed on 10.2), Python 3.11+ (uses the system `/usr/bin/python3.12`)
- Project-local packages: `pydantic>=2.7,<3`, `pyyaml>=6,<7`, `agent-framework-core==1.22.0`
  (Microsoft Agent Framework core only, with no cloud connectors), `fastapi==0.156.4` and
  `uvicorn==0.54.0`. For tests: `pytest>=8,<9` and `httpx==0.28.1`.
- Podman 5.x, rootless (developed with 5.8.2, crun, netavark, cgroup v2, SELinux enforcing)

## Setup

```bash
/usr/bin/python3.12 -m venv .venv
```

```bash
.venv/bin/python -m pip install "pydantic>=2.7,<3" "pyyaml>=6,<7" "agent-framework-core==1.22.0" \
    "fastapi==0.156.4" "uvicorn==0.54.0" "pytest>=8,<9" "httpx==0.28.1"
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

## Human approval

A saved diagnosis waits in `AWAITING_HUMAN_APPROVAL`. Review the package, which shows each
proposed action with its reason, supporting evidence, risk, affected component, confidence
and missing information:

```bash
./scripts/run_dev.sh review run-0123456789ab
```

Then record a decision:

- `approve` (the comment is optional);
- `reject` or `request-revision` (both need `--comment`);
- `revise`, which re-diagnoses the incident as a new run that supersedes the old one;
- `close`, after approval.

```bash
./scripts/run_dev.sh approve run-0123456789ab --operator ops.wang --comment "checks agreed"
```

Recording a decision **executes nothing**. Agent, bot and system identities are refused as
operators. `history <run>` lists a run's transitions, and `verify-audit` checks both hash
chains.

Records are written to `audit-data/`, or to `HPC_AGENT_STATE_DIR` if it's set:

- `runs/<run_id>.json`: write-once snapshots of each run;
- `approvals.jsonl` and `audit.jsonl`: append-only and hash-chained. Files are created with
  mode 0600.

## HTTP API and review page

Create an operator token. The token file must live outside the repository and have mode
0600. The token is printed once; only its hash is stored.

```bash
PYTHONPATH=src .venv/bin/python -m hpc_agent.main create-operator-token ops.yourname --tokens-file ~/.config/hpc-agent/operators.json
```

Run the API natively. It listens on 127.0.0.1 only:

```bash
HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json scripts/run_api.sh
```

Open http://127.0.0.1:8080/ and paste your token to use the review page.

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness check (no authentication) |
| `GET /api/whoami` | Operator identity derived from the token |
| `GET /api/incidents` | Fixture incident IDs |
| `GET /api/runs`, `POST /api/runs` | List runs; diagnose an incident (body `{"incident_id": …}`) |
| `GET /api/runs/{id}`, `GET /api/runs/{id}/review`, `GET /api/runs/{id}/history` | Run detail, approval package, transitions |
| `POST /api/runs/{id}/decision` | Body `{"decision": "APPROVED"\|"REJECTED"\|"REVISION_REQUESTED", "comment": …}`. The response includes `"executed": false`. |
| `POST /api/runs/{id}/close`, `POST /api/runs/{id}/revise` | Close an approved run; re-diagnose a rejected run |
| `GET /api/audit/verify` | Verify both hash chains |

Every `/api` endpoint requires `Authorization: Bearer <token>`. If no token file is
configured, they all return 503.

## Podman deployment

Regenerate the lockfile (only when dependencies change), then build:

```bash
scripts/lock_requirements.sh
```

```bash
podman build -f containers/Containerfile -t localhost/hpc-ai-agent:0.1.0 .
```

Run the container:

```bash
HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json scripts/podman_run.sh
```

Check the container's security posture and run one approval cycle:

```bash
scripts/container_smoke_test.sh
```

Container properties:

- **Image:** UBI 9 Python 3.12 base, pinned by digest. Dependencies are installed from a
  hash-locked file, wheels only. The image contains only `src/` and `config/`: no data, no
  credentials and no SSH keys.
- **User:** non-root, with the host UID mapped through `--userns=keep-id`.
- **Hardening:** read-only root filesystem with a small `/tmp` tmpfs, `--cap-drop=ALL`,
  `no-new-privileges`, and PID and memory limits.
- **Network:** the port is published on `127.0.0.1` only. No privileged mode, no host
  networking.
- **Volumes:** `fixtures/`, `test_data/` and `knowledge/` are mounted `ro,Z`. `audit-data/`
  is the only writable volume. The token-hash file is mounted `ro` from outside the
  repository.

## Known limitations

- Agents are deterministic rule-based analyzers. No LLM is integrated yet, and that needs a
  configured provider and credentials supplied by a human.
- The coordinator never confirms a root cause. Confirmation is left to the human reviewer
  until a confirmation rule is agreed and validated against representative incidents.
- Cross-domain correlation is rule-based. It links findings through shared incident items
  and shared components (node, job, OST, GPU, interface), and through timestamps within a
  fixed 30-minute window. The window is not yet tuned per site.
- Knowledge search uses keyword scoring, not semantic search. It sits behind the
  `KnowledgeBase` interface so a RAG backend can replace it later.
- The XID table covers a subset of NVIDIA XID codes. Unknown codes are reported as missing
  interpretation.
- Triage uses keyword signals, so unusual wording can be misclassified as `unknown`. That
  outcome is safe, but not useful.
- No API or UI yet.
- Operator identity is a naming-rule guard only; real authentication is future work.
- The credential scanner is pattern-based and can miss novel secret formats.
- On the CLI, `--operator` is asserted, not authenticated; the CLI is for a trusted local
  shell. The HTTP API derives identity from bearer tokens. There is no SSO, rate limiting or
  TLS: keep it on localhost, or put it behind an authenticating TLS reverse proxy before
  exposing it.
- The image is large (about 1.1 GB, mostly the UBI Python base) and includes an `ssh` client
  binary. Nothing exposes it, but a slimmer base would remove it.
- With `--userns=keep-id`, the first start on a new image copies and re-owns its layers.
  That took about 10 minutes on this busy host; later starts take about 1 second.
- The hash chain detects edited, reordered or deleted records, but not records removed from
  the end of the file. Anchor the latest hash externally if that matters.
- No MTTR claims are made. MTTR reduction may only be reported after measurement against
  representative incidents.

## Roadmap

1. ✅ Environment, models, fixtures, safety foundation, tests
2. ✅ Triage, Storage, Scheduler agents; Agent Framework workflow; end-to-end scenario runs
3. ✅ Network and GPU agents, RCA Coordinator, cross-domain correlation
4. ✅ Knowledge tools, temporal evidence correlation, runtime safety guards, call budget
5. ✅ Human-in-the-loop approval workflow, review package, hash-chained append-only audit logging
6. ✅ Authenticated HTTP API, minimal review page, hash-locked Containerfile, rootless Podman deployment
7. Evaluation, covering domain classification, root-cause accuracy, evidence quality,
   unsupported-claim rate, human override rate and processing time
