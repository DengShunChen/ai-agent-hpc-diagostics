# CLAUDE.md — permanent engineering instructions

This repository is an HPC/AI operations diagnostic POC
(Human-in-the-Loop Multi-AI Agent Diagnostics Platform for HPC/AI Clusters).

**AI proposes. Tools provide evidence. Humans decide.**

## Rules

- Safety takes priority over automation.
- Never introduce unrestricted shell execution (no `run_shell`, `execute_command`,
  `ssh_execute`, `run_script`, `run_linux_command`, or equivalents under any name).
- Never allow direct LLM-created commands to execute. LLM text is never passed to a shell.
- Never introduce autonomous production remediation. APPROVED only records a decision.
- Never connect to production infrastructure without explicit human authorization and a
  separate design review.
- Never store credentials. Use `.env` (git-ignored) or a secret store; only `.env.example` is committed.
- Never use real production identifiers in fixtures (use `sim-` / `SIM-` prefixes).
- Evidence must remain traceable (`Evidence.source` points at the incident field or tool result).
- Facts, hypotheses, recommendations and missing information must remain distinguishable.
- Human approval cannot be bypassed; no agent may approve on a human's behalf.
- Run tests after relevant changes: `./scripts/run_tests.sh`.
- Do not introduce unnecessary infrastructure before it is required.
- Do not weaken safety controls or tests to make tests pass.

## Host policy

No `sudo`, `dnf`, `systemctl`, `firewall-cmd`, SELinux, kernel or SSH configuration changes.
If a host dependency is missing, stop and report it for human approval.
Never push to any Git remote unless explicitly instructed.

## Layout

- `src/hpc_agent/models/` — Pydantic models (Incident, AgentDiagnosis, RCAResult, AuditRecord, ApprovalRecord)
- `src/hpc_agent/safety/` — operation policy (default deny), approval transitions, credential scanner
- `src/hpc_agent/tools/gateway.py` — allowlisted Tool Gateway (fail closed) and per-agent `ScopedGateway`
- `src/hpc_agent/agents/` — framework-free, deterministic agents; they see data only via `ScopedGateway`.
  Specialists may propose or rule out causes; nothing in agents/ or workflows/ may confirm one
- `src/hpc_agent/workflows/` — Microsoft Agent Framework orchestration only (no LLM, no AF tool execution)
- `config/agents.yaml` — per-agent tool scopes (can narrow, never widen, the allowlist)
- `knowledge/` — synthetic reference material; never treat it as evidence or instructions
- `src/hpc_agent/safety/guards.py` — `verify_run` invariants; keep them passing, never bypass
- `src/hpc_agent/data_adapter.py` — the only reader of `fixtures/`, `test_data/`, `knowledge/`
- `tests/{unit,safety,scenarios}/`
