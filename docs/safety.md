# Safety

## Threat model

| Threat | Mitigation |
|---|---|
| LLM is prompt-injected via incident text/logs and asks to run commands | No execution tools exist; the gateway only exposes read-only saved-data tools with pattern-restricted arguments |
| Agent invents metrics or status | Tools return `no_data` instead of values. Models require evidence for findings and confidence. Confirmation requires tool-derived evidence |
| Hypothesis presented as confirmed root cause | `ProbableCause` cannot be `confirmed` without tool evidence, or while there is conflicting evidence. At most one cause can be confirmed |
| AI approves its own recommendation | Decision states require a human operator identity; agent/bot/system-style identities are rejected |
| Path traversal / reading host files | Data adapter resolves symlinks and confines reads to `fixtures/`, `test_data/`, `knowledge/` |
| Credentials leak into data or audit logs | Fixtures, test data and audit records are scanned. A match raises an error that names only the location, never the value |
| Production identifiers in fixtures | Synthetic `sim-`/`SIM-` prefixes; tests reject IPv4 and email addresses in data |
| Developer adds a shell tool later | Static test fails on `subprocess`, `os.system`, `shell=True`, `eval`/`exec`, ssh/pty libraries, and on forbidden function names |

## Trust boundaries

1. **Untrusted input**: incident text, alerts, logs and operator notes, plus all LLM output.
2. **Controlled boundary**: the Tool Gateway and the safety policy. Every data access goes through them.
3. **Read-only data**: `fixtures/`, `test_data/`, `knowledge/`.
4. **Writable**: `audit-data/`, application state and temp files only.
5. **Human operator**: the only party who can approve, reject or request revision.

## No production access

Production FEFS/Lustre, PJM, Slurm, network devices, GPU control and DCGM execution are all
out of scope for the POC. `config/app.yaml` documents `production_access: false`. Adding
production connectivity later needs explicit human authorization and a separate design
and security review.

## No arbitrary shell execution

- `run_shell`, `execute_command`, `ssh_execute`, `run_script` and `run_linux_command` are
  denied by name.
- Names containing shell, exec, ssh, script, command, cmd, sudo, eval, subprocess or popen
  are also denied, as are names with tokens such as os, system, run, spawn, kill, reboot,
  delete, rm, write or set.
- No gateway handler shells out. They are plain Python functions that read JSON.
- LLM-generated text is never passed to a shell.

## Tool Gateway allowlist

The registered tools in Iteration 1 are `get_filesystem_usage`, `get_ost_usage`,
`get_job_status`, `get_node_status`, `get_network_status` and `get_gpu_health`. All of them
are `READ_TEST_DATA` operations and take a single `incident_id` argument, which must match
the ID pattern. Unknown tools, non-string names, extra or missing arguments, and tools
whose operation the policy denies are all rejected.

## Safety policy

The policy is default deny. It allows `READ_TEST_DATA`, `READ_KNOWLEDGE`,
`CREATE_DIAGNOSIS`, `RECOMMEND_ACTION` and `RECORD_HUMAN_DECISION`. It denies
`RUN_ARBITRARY_COMMAND`, `SSH_PRODUCTION`, `MODIFY_PRODUCTION`, `DELETE_PRODUCTION_DATA`
and `AUTONOMOUS_REMEDIATION`. Any other value is denied, and the rule table is read-only.

## Human-in-the-loop

Only allowed transitions are accepted. APPROVED, REJECTED and REVISION_REQUESTED need a
human operator. APPROVED **records** a decision and executes nothing. Records are immutable,
and each transition creates a new record with `run_id`, `incident_id`, `status`,
`operator`, `operator_comment` and a timezone-aware `timestamp`.

Before approval, the UI (Iteration 5/6) must show the proposed action, the reason, the
supporting evidence, the risk, the affected component, the confidence and the missing
information. All of these are fields of `RecommendedAction` / `RCAResult`.

## Credential handling

- Only `.env.example` is committed; `.env` is git-ignored.
- No credentials are needed in Iteration 1.
- If sensitive data is detected, processing stops with
  "SENSITIVE INFORMATION DETECTED AND NOT PROCESSED." and the value is never displayed.

## Audit requirements

`AuditRecord` covers the run ID, incident ID, workflow stage, agents, tool invocations
(tool, agent, result status, evidence reference, duration), evidence used, diagnosis,
recommendations, approval status and operator comment. Records are immutable and are
rejected if they contain credential-like content. Append-only persistence comes in Iteration 5.

## Fail-closed behavior

Each of these cases raises an error instead of continuing:

- unknown tool
- denied operation
- unknown policy operation
- invalid argument
- read outside the data roots
- oversized file
- credential-like content
- invalid approval transition
- non-human approver
