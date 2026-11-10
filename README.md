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

## Goals and where they stand

The project aims to cut fault-localization time for cross-layer HPC/GPU incidents with
collaborating triage, diagnosis and remediation agents. It keeps operations safe through
human approval, is designed to roll out to other data-center operations teams in East
Asia, and is meant to grow into a Uvance AIOps customer proposal.

| Goal | What exists now | What is still needed |
|---|---|---|
| Multi-agent root-cause analysis across network, storage and scheduler layers | Triage, storage/scheduler/network/GPU diagnosis, RCA Coordinator, **remediation agent**, all coordinated by Microsoft Agent Framework. Triage and the specialists can be **LLM-driven** (`HPC_AGENT_REASONING=llm`): the model picks tools and proposes; deterministic checks keep only grounded proposals | A model that beats the rules: on the synthetic cases, a local 7B model on CPU scores well below the rules ([docs/evaluation-llm.md](docs/evaluation-llm.md)) |
| Operational safety | Human approval, no execution path, allowlisted read-only tools, runtime guards, hash-chained audit | Production security review |
| Shorter fault localization (Business Impact) | `hpc-agent metrics` (workflow timings, override rate), [measurement protocol](docs/business-impact.md) | Baseline and pilot measurement. **No reduction is claimed.** |
| Rollout across sites (Replicability) | Site profiles (`config/sites/`), per-site metrics, a single container image, the [site rollout guide](docs/site-replication.md), an offline **export importer** with a data contract and de-identification (`hpc-agent import-export`) | Each site's export procedure (security review); cases labelled by each site |
| Uvance AIOps proposal | [Customer proposal draft](docs/aiops-proposal.md) (internal, with honest evidence status) | Pilot results and business/legal review |

## POC scope

- Offline proof of concept on a single Rocky Linux 10 server.
- Synthetic, de-identified data only (`fixtures/`, `test_data/`, `knowledge/`).
- Production HPC, storage, scheduler, network and GPU access is **disabled**.
- No SSH automation, no sudo for agents, no shell tools, no automatic remediation.

**Current status: all seven POC iterations are complete.** All five specialist roles and the RCA Coordinator
run end to end through a Microsoft Agent Framework workflow on synthetic data. The workflow
produces a ranked, evidence-linked RCA with a timeline, and it never confirms a root cause on
its own.

- Agents cite SOPs, manuals and past incidents from the knowledge base as references only.
- Every run is checked against runtime safety invariants before it is returned.
- Triage and the four specialists run in one of two modes. With `HPC_AGENT_REASONING=llm`, a
  local model (Ollama) chooses which read-only tools to call and proposes hypotheses, and
  deterministic checks keep only proposals grounded in the data it fetched (see "LLM-driven
  agents"). With `rules` (the default), deterministic agents do the work; they
  are also the fallback. A local LLM can also write a Traditional Chinese explanation of a
  run's evidence (see "Local LLM explainer").
- Every saved run waits for a human decision: approve, reject or request revision. Decisions
  are recorded in a tamper-evident audit trail and are never executed.
- An authenticated HTTP API and a minimal review page serve the same workflow. Operators
  authenticate with bearer tokens, and only token hashes are stored.
- The app ships as one rootless, non-root Podman container with a read-only root filesystem
  and read-only data volumes; `audit-data/` is its only writable volume.
- An evaluation harness scores the system on 13 labelled synthetic cases; see
  [docs/evaluation.md](docs/evaluation.md). A scripted demo is in [docs/demo.md](docs/demo.md).
  **No MTTR reduction is claimed.**

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
| Remediation (處置) | ✅ | Proposes playbook remediation, with pre-action verification, rollback and an SOP reference, only for tool-backed hypotheses at or above the site's confidence threshold; lists deferred hypotheses with reasons; no tools; never executes |

## Workflow and approval states

1. An incident from `fixtures/` is diagnosed. The CLI, the API or the review page can start
   this.
2. **Triage** classifies the incident and selects specialists.
3. The selected **specialists** gather evidence through their scoped, read-only tools.
4. The **RCA Coordinator** ranks the hypotheses, relates domains and builds the timeline.
5. The **remediation agent** proposes playbook actions for well-supported hypotheses and
   defers the rest, giving a reason for each.
6. The **runtime guard** verifies the run's invariants.
7. The run is saved, and its approval state moves
   `DRAFT → DIAGNOSIS_COMPLETE → AWAITING_HUMAN_APPROVAL`.
8. A **human** decides: APPROVED (then CLOSED), REJECTED or REVISION_REQUESTED. Rejection and
   revision requests lead to REVISE, and the incident is re-diagnosed as a new run.
9. Each step is written to the hash-chained audit trail. **Nothing is ever executed.**

Thresholds come from the **site profile** (`HPC_AGENT_SITE`, default `default`). Workflow
metrics per site come from `hpc-agent metrics`.

## Repository structure

```
config/            agents/*.md (agent definitions: tool scopes, LLM prompt and limits), app.yaml
src/hpc_agent/
  models/          Incident, AgentDiagnosis, RCAResult, AuditRecord, ApprovalRecord, ApprovalRequest,
                   TriageResult, DiagnosticRun
  agents/          triage, storage, scheduler, network, gpu, coordinator (RCA), evidence helpers
  workflows/       diagnostic_workflow.py (Agent Framework orchestration, report),
                   approval_workflow.py (human-in-the-loop service, run store, review)
  tools/           gateway.py (allowlist, ScopedGateway, call budget), per-domain tools, knowledge_tools.py
  safety/          policy (default deny), approval (state machine), guards (run invariants),
                   operators (API token auth), secrets (credential scanner)
  logging/         audit_logger.py (hash-chained append-only JSONL)
  api/             routes.py (FastAPI) + static/ (review page)
  evaluation/      harness.py (metrics), report.py (Markdown report, scenario comparison)
  ingest/          contract.py (data contract), deidentify.py, importer.py, csv_export.py (reference adapter)
  config.py, data_adapter.py, main.py (CLI)
fixtures/incidents/                 synthetic POC incidents
test_data/<domain>/<incident_id>/   saved synthetic tool outputs
knowledge/{incidents,sop,manuals}/  synthetic knowledge base (reference material only)
evaluation/cases/                   labelled synthetic evaluation cases
examples/site-export/               synthetic CSV export for `hpc-agent import-export`
tests/{unit,safety,scenarios}/
containers/        Containerfile, requirements.lock (hash-locked)
scripts/           run_tests, run_dev, run_api, demo, lock_requirements, podman_run, container_smoke_test
docs/              architecture, safety, scenarios, evaluation (generated), demo,
                   business-impact, site-replication, pilot-runbook (zh-TW), aiops-proposal (zh-TW)
config/sites/      site profiles (diagnostic thresholds per site)
audit-data/        runtime state and audit logs (git-ignored)
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
Each agent's definition (`config/agents/<name>.md`) sets which gateway tools it may call. It can only narrow the
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

## Evaluation

```bash
PYTHONPATH=src .venv/bin/python -m hpc_agent.main evaluate --output docs/evaluation.md
```

This runs every case in `evaluation/cases/` through the full workflow and reports the
metrics from spec §21:

- domain classification;
- root-cause accuracy (top-1 and top-3);
- outcome and refusal correctness;
- evidence quality;
- unsupported-claim rate;
- human override rate, only when you pass `--approvals-log` with real decisions;
- processing time.

The cases are synthetic and were labelled by the developer, so the numbers show designed
behavior, not field accuracy. **MTTR is not measured or claimed.**

## Demo

```bash
./scripts/demo.sh
```

The demo walks through all three scenarios, the refused AI approval, the revision cycle,
tamper detection and the evaluation summary. Add `DEMO_PAUSE=1` to pause between steps. The
presenter notes are in [docs/demo.md](docs/demo.md).

## LLM-driven agents (optional)

Two model providers (hybrid): a local Ollama model (default; `qwen2.5:7b` or larger is recommended)
or Claude through the official `anthropic` SDK. Claude is used only when selected, only on data whose
`fixtures/DATA_CLASS.json` says `synthetic` (the POC data) or `deidentified-import` (written by
`hpc-agent import-export`), and every request is scanned for credentials and identifiers before it
leaves; a blocked request falls back to the rule-based agent. Claude requests are billed.

```bash
MODEL_PROVIDER=anthropic HPC_AGENT_REASONING=llm PYTHONPATH=src .venv/bin/python -m hpc_agent.main diagnose POC-SCHEDULER-001 --no-save
```

`MODEL_NAME` defaults to `claude-opus-5-5`, `HPC_AGENT_CLAUDE_EFFORT` to `high`; the SDK reads
`ANTHROPIC_API_KEY` (or an `ant auth login` profile). Requests use server-side refusal fallback.

```bash
MODEL_PROVIDER=ollama MODEL_NAME=qwen2.5:7b HPC_AGENT_REASONING=llm PYTHONPATH=src .venv/bin/python -m hpc_agent.main diagnose POC-SCHEDULER-001 --no-save
```

- Triage: the model picks the domains and cites the incident items behind each. The keyword
  rules may add a domain the model missed, never remove one.
- Specialists: each is offered only the tools in its scope, and decides which to call. Tool
  arguments never come from the model (data tools always get this incident's ID).
- Every hypothesis must cite fetched tool values by field and value. The evidence statement and
  components are written from the matched data. Categories are fixed, confidence is capped at
  0.6, nothing can be confirmed, and text with ungrounded numbers, commands, confirmation claims
  or credential-like content is discarded.
- Discarded proposals and each agent's reasoning mode (`llm`, `rules`, `rules-fallback`) appear in
  the report and on the review page.
- The RCA Coordinator and the remediation agent stay deterministic.

Measured on the 13 synthetic cases on this CPU-only host ([docs/evaluation-llm.md](docs/evaluation-llm.md)):
qwen2.5:7b scored 36–55% top-1 root cause across runs (100% for the rules), and a one-line prompt
change moved its results more than switching between 3B and 7B. Every kept proposal cited fetched data
or an incident item, and the current runs had no unsupported claims under the harness definition (an
earlier 3B run had 2 grounded but wrong ones). A diagnosis takes about 3 minutes with 7B. The next real
test is a larger model (14B+) on a host with a GPU or more memory. Until a model beats the rules
on independently labelled incidents, `rules` stays the recommended mode for review work.

## Local LLM explainer (optional)

Requires a local Ollama with a local model, for example `ollama pull qwen2.5:3b`.

```bash
MODEL_PROVIDER=ollama MODEL_NAME=qwen2.5:3b PYTHONPATH=src .venv/bin/python -m hpc_agent.main explain run-0123456789ab
```

In the review page, set the same variables before starting the API or container, then use
**產生 AI 說明** ("generate AI explanation").

What the LLM gets and may do:

- It receives the run's numbered evidence (E), hypotheses (H), ruled-out causes (R),
  missing information (M) and checks (C). Incident text is marked as data, not instructions.
- It gets no tools and can't change the diagnosis, confidence, recommendations or approval
  state.

Its answer is rejected as a whole, and never shown, if:

- any sentence lacks a valid citation;
- any number doesn't appear in the evidence;
- it contains command-like text;
- it claims a confirmed root cause;
- it contains anything credential-like;
- a sentence about ruled-out causes doesn't cite an R item, or claims something was ruled out
  when nothing was;
- it contains Simplified Chinese characters (the output must be Traditional Chinese).

Measured with qwen2.5:3b on CPU (deterministic decoding), 1 of 3 POC explanations passed,
taking 19–38 s each. The rejections were real:
- STORAGE described the missing-information items as "ruled-out causes";
- SCHEDULER used the Simplified character 「达」;
- an earlier GPU attempt left a sentence uncited.

Adding these rules to the *prompt* made the model worse (0 of 3), so they are enforced only
by validation.

Every attempt, accepted or rejected, is stored in the hash-chained `audit-data/explanations.jsonl`
and logged in the audit trail.

The endpoint must be local, and Ollama `:cloud` models are refused unless you set
`HPC_AGENT_LLM_ALLOW_CLOUD=1`.

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

For a pilot, mount imported data (see `hpc-agent import-export`) instead of the synthetic
fixtures, and select the site profile. The procedure is in
[docs/pilot-runbook.md](docs/pilot-runbook.md):

```bash
HPC_AGENT_DATA_DIR=~/hpc-pilot/data HPC_AGENT_SITE=<site-id> HPC_AGENT_STATE_DIR=~/hpc-pilot/audit-data HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json scripts/podman_run.sh
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

- **LLM-driven agents are an option, not yet better than the rules.** With
  `HPC_AGENT_REASONING=llm` the model chooses tools and forms hypotheses, but local 3B/7B models
  on CPU score well below the rule-based agents on the synthetic cases, and the checks verify
  that evidence exists, not that the reasoning about it is right (a 3B run cited a real 83%
  usage as a capacity problem). The fact matcher still rejects some grounded citations, for
  example a record ID written as `sim-gpu-node-0007 GPU 3`. See
  [docs/evaluation-llm.md](docs/evaluation-llm.md).
- A small local model on CPU is slow (tens of seconds to minutes per explanation), and its
  output is often rejected by the grounding checks. That's by design: an unsupported
  explanation is never shown.
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
- The export importer pseudonymizes host, job and filesystem names and rejects IP addresses,
  e-mail addresses, domain names and home paths, but it can't recognise every personal or
  customer detail in free text (symptoms, logs, notes). A person must review those before use.
- On the CLI, `--operator` is asserted, not authenticated; the CLI is for a trusted local
  shell. The HTTP API derives identity from bearer tokens. There is no SSO, rate limiting or
  TLS: keep it on localhost, or put it behind an authenticating TLS reverse proxy before
  exposing it.
- The image is large (about 1.1 GB, mostly the UBI Python base) and includes an `ssh` client
  binary. Nothing exposes it, but a slimmer base would remove it.
- With `--userns=keep-id`, the first start on a new image copies and re-owns its layers.
  That took about 10 minutes on this busy host; later starts take about 1 second.
- Triage classification is keyword-based, so ties are possible. The RCA classification now
  follows tool evidence when there is any. This rule was added **after** the evaluation
  showed two storage cases misclassified as `multi-domain` (85% before the change). Results on
  those cases therefore don't independently test it; see the disclosure in
  [docs/evaluation.md](docs/evaluation.md).
- The evaluation labels are synthetic and were written by the developer of the agents.
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
7. ✅ Evaluation harness and report, scenario comparison, demo script and guide

**Beyond the POC** (each step needs separate authorization and a security review):

1. Independent evaluation on representative, de-identified incidents labelled by operations
   engineers, plus override rates from real reviews.
2. Read-only production data adapters behind the Tool Gateway (FEFS/Lustre, PJM/Slurm, DCGM
   exports, SNMP and monitoring), connected over a private network.
3. An optional LLM layer that phrases findings from existing evidence, validated by the same
   models and guards. It would get no new tools.
4. SSO and TLS for the API, external anchoring of the audit hash, and a slimmer base image.
5. Only after measurement: MTTR comparison against the current process.
