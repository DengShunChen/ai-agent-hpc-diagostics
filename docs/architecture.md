# Architecture

## Logical flow

```mermaid
flowchart TD
    A[Incident / Alert] --> UI[Web UI / API]
    UI --> T[Triage Agent]
    T --> S[Storage Agent]
    T --> SC[Scheduler Agent]
    T --> N[Network Agent]
    S --> G[GPU Agent]
    SC --> G
    N --> G
    G --> C[RCA Coordinator]
    C --> R[RCA Assessment<br/>facts / hypotheses / missing info]
    R --> REC[Recommendations]
    REC --> H{Human Approval}
    H -->|APPROVED| REC_ONLY[Decision recorded only<br/>nothing executed]
    H -->|REJECTED / REVISION_REQUESTED| REV[REVISE]

    subgraph Controlled data access
        TG[Tool Gateway<br/>explicit allowlist, fail closed]
        P[Safety Policy<br/>default deny]
        DA[Read-only Data Adapter]
        FX[(fixtures/)]
        TD[(test_data/)]
        KN[(knowledge/)]
        TG --> P
        TG --> DA
        DA --> FX & TD & KN
    end

    S & SC & N & G -.->|invoke tool| TG
    C -.-> AUD[(Audit records<br/>audit-data/)]
    H -.-> AUD
```

## Deployment (target, Iteration 6)

```
Rocky Linux 10
 └─ Podman (rootless)
     └─ hpc-ai-agent container (non-root)
         ├─ API, agent workflow (triage, specialists, RCA coordinator)
         ├─ Tool Gateway, knowledge layer, human approval, audit logger
         └─ test data adapter
     volumes: knowledge/ (ro), test_data/ (ro), audit-data/ (rw)
```

## Components implemented in Iteration 1

| Component | Module |
|---|---|
| Incident model | `models/incident.py` |
| AgentDiagnosis, RCAResult, Evidence, ProbableCause, RecommendedAction | `models/diagnosis.py` |
| ApprovalRecord, ApprovalStatus | `models/approval.py` |
| AuditRecord | `models/audit.py` |
| Approval state machine | `safety/approval.py` |
| Safety policy (default deny) | `safety/policy.py` |
| Credential-like content scanner | `safety/secrets.py` |
| Read-only data adapter | `data_adapter.py` |
| Tool Gateway + read-only domain tools | `tools/gateway.py`, `tools/*_tools.py` |

## Components added in Iteration 2

| Component | Module |
|---|---|
| Triage Agent | `agents/triage.py` |
| Storage Agent | `agents/storage.py` |
| Scheduler Agent | `agents/scheduler.py` |
| Evidence helpers, domain keyword signals | `agents/evidence.py` |
| Per-agent ScopedGateway (scope ∩ allowlist, records every call) | `tools/gateway.py` |
| Agent definitions and tool scopes | `config/agents/*.md`, `runtime/definitions.py`, `config.py` |
| TriageResult, DiagnosticRun | `models/triage.py`, `models/run.py` |
| Agent Framework workflow + text report | `workflows/diagnostic_workflow.py` |

## Components added in Iteration 3

| Component | Module |
|---|---|
| Network Agent | `agents/network.py` |
| GPU Agent (XID table, telemetry checks; no live DCGM) | `agents/gpu.py` |
| RCA Coordinator | `agents/coordinator.py` |
| Structured causes on `AgentDiagnosis` (`ProbableCause` with domain, support, conflicts) and `affected_components` | `models/diagnosis.py` |
| `RCAResult.cross_domain_relationships`; `DiagnosticRun.rca` | `models/diagnosis.py`, `models/run.py` |

### Workflow (Iteration 3)

```mermaid
flowchart LR
    I[Incident] --> T[TriageExecutor]
    T -->|multi-selection edge:<br/>selected_agents| S[storage-agent]
    T --> SC[scheduler-agent]
    T --> N[network-agent]
    T --> G[gpu-agent]
    T -->|TriagedIncident:<br/>expected count| C[CoordinatorExecutor]
    S & SC & N & G -->|AgentDiagnosis| C
    C --> O[RCAResult<br/>ranked hypotheses, never confirmed]
```

### RCA Coordinator rules

- **Ranking**: open hypotheses are sorted by confidence. Ties keep triage order and are
  reported as ties in the summary.
- **Ruled out**: causes that specialists ruled out are kept, with the conflicting evidence
  they cite.
- **Classification**, decided by tool evidence first:
  - `multi-domain` when tool-backed hypotheses span two or more domains;
  - that domain when they span exactly one;
  - the keyword-based triage classification only when no hypothesis is tool-backed.

  The single-domain rule was added after the evaluation (see [evaluation.md](evaluation.md)).
- **Relationships**: an incident item cited by two or more domains, or a component (for
  example `node:…`) implicated by two or more domains.
- **Cross-domain checks**: when a node is implicated, checks are recommended for any storage,
  network or GPU domain that wasn't examined.
- **Never confirms.** Specialists can't confirm (enforced by the model), and the coordinator
  doesn't. A static test checks that no agent or workflow code sets `CONFIRMED`.

## Components added in Iteration 5

| Component | Module |
|---|---|
| `ApprovalRequest`, `ActionForReview`; `AuditRecord.operator`; `DiagnosticRun.supersedes` | `models/approval.py`, `models/audit.py`, `models/run.py` |
| Approval service, run store, review rendering | `workflows/approval_workflow.py` |
| Hash-chained append-only log | `logging/audit_logger.py` |
| Operator CLI (`review`, `approve`, `reject`, `request-revision`, `revise`, `close`, `history`, `verify-audit`) | `main.py` |

```mermaid
stateDiagram-v2
    [*] --> DRAFT: diagnose (system)
    DRAFT --> DIAGNOSIS_COMPLETE
    DIAGNOSIS_COMPLETE --> AWAITING_HUMAN_APPROVAL
    AWAITING_HUMAN_APPROVAL --> APPROVED: human
    AWAITING_HUMAN_APPROVAL --> REJECTED: human + comment
    AWAITING_HUMAN_APPROVAL --> REVISION_REQUESTED: human + comment
    APPROVED --> CLOSED: human
    REJECTED --> REVISE: human
    REVISION_REQUESTED --> REVISE: human
    REVISE --> [*]: new run (supersedes)
```

## Components added in Iteration 6

| Component | Module |
|---|---|
| HTTP API (FastAPI) | `api/routes.py` |
| Review page (static HTML/CSS/JS, no framework) | `api/static/` |
| Operator token authentication | `safety/operators.py` |
| Container image | `containers/Containerfile`, `containers/requirements.lock`, `.containerignore` |
| Deployment and verification scripts | `scripts/podman_run.sh`, `scripts/container_smoke_test.sh`, `scripts/lock_requirements.sh`, `scripts/run_api.sh` |

### Deployed form

```mermaid
flowchart LR
    B[Operator browser / curl] -->|127.0.0.1:8080<br/>Bearer token| C
    subgraph C[hpc-ai-agent container: rootless, non-root, read-only rootfs, no caps]
        API[FastAPI routes] --> SVC[ApprovalService]
        SVC --> WF[Diagnostic workflow<br/>agents + Tool Gateway]
    end
    FX[(fixtures/ ro)] --> WF
    TD[(test_data/ ro)] --> WF
    KN[(knowledge/ ro)] --> WF
    TOK[(operators.json ro<br/>hashes, outside repo)] --> API
    SVC --> AD[(audit-data/ rw<br/>hash-chained logs)]
```

## Evaluation (Iteration 7)

`evaluation/harness.py` runs each labelled case through the real workflow, in an isolated
temporary data root, and scores it. `evaluation/report.py` renders `docs/evaluation.md`,
including a comparison table across the three POC scenarios. Cause categories
(`ProbableCause.category`, for example `storage.capacity`) give stable labels for scoring.
Human override rates come only from recorded approval logs.

## Remediation agent and site profiles

```mermaid
flowchart LR
    T[Triage] --> S[storage / scheduler / network / GPU agents]
    S --> C[RCA Coordinator]
    C --> R[Remediation agent<br/>playbook + site threshold]
    R --> P[DiagnosticRun<br/>rca.recommended_actions + remediation plan]
    P --> H{Human approval}
    SP[(config/sites/&lt;site&gt;.yaml)] -.thresholds.-> S
    SP -.window.-> C
    SP -.min confidence.-> R
```

- **Remediation agent** (`agents/remediation.py`):
  - It proposes a `RecommendedAction` only for open, tool-backed hypotheses at or above
    `remediation_min_confidence`, using its playbook. Each action carries the hypothesis
    it addresses, the checks to verify first, a rollback and an SOP reference. The
    affected components come from the hypothesis (`ProbableCause.components`).
  - Every other open hypothesis is listed in `RemediationPlan.deferred`, with the reason.
  - It has no tools, and the plan is typed `executes: false`. The diagnosis agents no
    longer produce actions.
- **Site profiles** (`models/site.py`, `config/sites/*.yaml`, selected by `HPC_AGENT_SITE`):
  - They set the storage full and imbalance thresholds, the GPU thermal limit, the
    correlation window, the call budget and the remediation confidence.
  - Every value is bounded. There are no fields for policy, tools or approval, and unknown
    fields are rejected.
- **Workflow metrics** (`metrics.py`, `hpc-agent metrics`, `GET /api/metrics`): timings
  and override rate per site, taken from the run snapshots and the approval log. These are
  not MTTR.

## Design notes

- **Evidence traceability**: every `Evidence` has a `kind` (incident report, tool result or
  knowledge) and a `source` reference. Tool results carry the relative path of the saved
  data they came from.
- **No invented data**: if a tool has no saved data, it returns `no_data` with `data=None`.
- **Framework boundary**: agents are plain Python classes. Microsoft Agent Framework
  (`agent-framework-core`) is used only in `workflows/` to route messages between executors.
  An exception in any agent aborts the run (fail closed).
- **LLM-driven agents** (`agents/llm_agents.py`, `HPC_AGENT_REASONING=llm`): triage and the
  four specialists are driven by a local model. Each specialist is offered only the tools of
  its own scope and decides which to call and what to propose. What a human sees is decided
  by deterministic checks:
  - Tool arguments never come from the model: data tools always get this incident's ID; a
    knowledge query must pass the gateway's argument pattern. Calls go through the agent's
    `ScopedGateway`, so the scope, call budget and audit log apply unchanged.
  - Every fact must cite a tool result the agent fetched (`T#`), by field and exact value.
    The evidence statement and the affected components are written from the matched data,
    never from the model's words.
  - Categories come from a fixed per-domain list. Confidence is capped (0.6 with tool facts,
    0.3 with reports only). Nothing can be confirmed; ruling a cause out needs tool data.
  - Text with numbers that are not in the data, commands, confirmation claims or
    credential-like content is discarded. Discarded proposals are listed for the reviewer.
  - If the model is unavailable or its answer is unusable, the rule-based agent runs instead
    (`reasoning: rules-fallback`). Triage keywords may add a domain the model missed, never
    remove one.
  - The RCA Coordinator and the remediation agent stay deterministic: correlation and the
    playbook are not delegated to the model.
- **Rule-based agents** (`HPC_AGENT_REASONING=rules`, the default): the
  original deterministic agents. They are also the fallback, and the baseline the LLM
  agents are evaluated against.
- **Model providers (hybrid)**: `llm/provider.py` (local Ollama, the default) and `llm/claude.py`
  (Claude through the official `anthropic` SDK). Both implement the same `chat`/`generate` interface, so
  the harness and the checks don't change. Claude is gated by the data class and an outbound scan (see
  docs/safety.md).
- **Local LLM explainer** (`llm/provider.py`, `llm/explainer.py`): an optional
  `LLMProvider` with a local-only `OllamaProvider` (standard-library HTTP, no new
  dependencies). It writes a Traditional Chinese explanation of an existing run.
  - It runs after a diagnosis, on request through the API or CLI. It has no tools, and a test
    enforces that the `llm/` package has no access to the gateway, data adapter or approval code.
  - Its output is validated deterministically and rejected as a whole on any failure. It is
    stored separately (`explanations.jsonl`) and never changes a run.
  - The provider is selected by `MODEL_PROVIDER` / `MODEL_NAME` / `MODEL_ENDPOINT`.
    Agent Framework's Ollama connector could replace the client behind the same interface.
- **Knowledge layer**: `search_incident_history`, `search_sop` and `search_manual` are
  implemented by `KeywordKnowledgeBase` (`tools/knowledge_tools.py`).
  - It scores documents by distinct matching terms, with title terms counting double, and
    ignores identifiers such as node names.
  - It needs at least two matching terms, and keeps only hits scoring at least half of the
    best one.
  - The `KnowledgeBase` protocol lets a RAG backend replace it without changing callers.

## Components added in Iteration 4

| Component | Module |
|---|---|
| Synthetic SOPs, manuals, past incidents | `knowledge/` |
| Knowledge tools and `KnowledgeBase` interface | `tools/knowledge_tools.py` |
| Read-only Markdown access (`read_text`, `list_files`) | `data_adapter.py` |
| `query` argument type, per-run `CallBudget` | `tools/gateway.py` |
| Reference lookup from an agent's own findings | `agents/evidence.py` (`gather_references`) |
| `Evidence.observed_at`; `references` on diagnoses and RCA; `RCAResult.timeline` | `models/diagnosis.py` |
| Temporal correlation | `agents/coordinator.py` |
| Run-level invariant checks | `safety/guards.py` |

### Temporal correlation

When an observation in the saved data has a timestamp, the agent records it as `observed_at`.
The coordinator then:

- builds a timeline;
- reports observations within ±30 minutes of the incident as concurrent, and flags those
  more than 30 minutes before it as of uncertain relevance;
- flags observations more than 30 minutes after the incident: they can't have caused it and
  may not reflect conditions at the time of failure;
- halves the confidence of a hypothesis when all of its tool evidence comes from after the
  window.

If the incident has no timestamp, no temporal claims are made.
