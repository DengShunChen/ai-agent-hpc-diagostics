#!/usr/bin/env bash
# End-to-end demo of the POC (offline, synthetic data). Uses a throwaway state directory.
#   scripts/demo.sh              run straight through
#   DEMO_PAUSE=1 scripts/demo.sh pause between steps (for a live presentation)
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
export HPC_AGENT_STATE_DIR="$(mktemp -d)"
trap 'rm -rf "$HPC_AGENT_STATE_DIR"' EXIT
cli() { .venv/bin/python -m hpc_agent.main "$@"; }
step() { printf '\n\033[1m==== %s ====\033[0m\n' "$*"; if [ "${DEMO_PAUSE:-0}" = 1 ]; then read -rp "(enter) "; fi; }
section() { sed -n "/^$1/,/^\$/p"; }

step "1. Offline data and the tool allowlist"
cli

step "2. The Tool Gateway refuses shell-style tools (fail closed)"
.venv/bin/python - <<'PY'
from hpc_agent.tools.gateway import build_default_gateway
gateway = build_default_gateway()
for name in ("run_shell", "execute_command", "ssh_execute"):
    try:
        gateway.invoke(name, command="id")
    except PermissionError as error:
        print(f"refused: {error}")
PY

step "3. Scenario 01 (FEFS/Lustre write failure): the platform refuses an unsupported conclusion"
out="$(cli diagnose POC-STORAGE-001)"
echo "$out" | section "SUMMARY"
echo "$out" | section "HYPOTHESES"
echo "$out" | section "MISSING INFORMATION"
echo "$out" | section "RECOMMENDED CHECKS"
RUN1="$(echo "$out" | grep -o 'run-[0-9a-f]\{12\}' | tail -1)"

step "4. Human review: an AI identity cannot approve; a rejection needs a reason"
cli approve "$RUN1" --operator rca-coordinator || true
cli request-revision "$RUN1" --operator ops.demo || true
cli request-revision "$RUN1" --operator ops.demo --comment "collect per-OST utilization first"
cli revise "$RUN1" --operator ops.demo

step "5. Scenario 02 (scheduler job failure): uncertainty kept, walltime ruled out, timeline"
out="$(cli diagnose POC-SCHEDULER-001)"
echo "$out" | section "HYPOTHESES"
echo "$out" | section "RULED OUT"
echo "$out" | section "CROSS-DOMAIN"

step "6. Scenario 03 (GPU node error): no hardware conclusion; approve the assessment"
out="$(cli diagnose POC-GPU-001)"
echo "$out" | section "FINDINGS"
RUN3="$(echo "$out" | grep -o 'run-[0-9a-f]\{12\}' | tail -1)"
cli review "$RUN3" | sed -n '1,12p'
cli approve "$RUN3" --operator ops.demo --comment "agree: investigate application first"
cli close "$RUN3" --operator ops.demo
cli history "$RUN3"

step "7. Tamper-evident audit trail"
cli verify-audit
copy="$(mktemp -d)"; cp "$HPC_AGENT_STATE_DIR"/*.jsonl "$copy"/
echo "Editing a copy: rewrite who approved the first human decision (ops.demo -> ops.mallory)..."
sed -i '0,/"operator":"ops\.demo"/s//"operator":"ops.mallory"/' "$copy/approvals.jsonl"
grep -q '"ops.mallory"' "$copy/approvals.jsonl" || { echo "demo error: edit did not apply" >&2; exit 1; }
if HPC_AGENT_STATE_DIR="$copy" cli verify-audit; then echo "demo error: tampering not detected" >&2; exit 1; fi
echo "(edited copy rejected, as expected)"
rm -rf "$copy"

step "8. Evaluation on labelled synthetic cases (no MTTR claims)"
cli evaluate | sed -n '/^## Summary/,/^## Per-case/p' | head -n -2
echo
echo "Demo complete. Nothing was executed on any system; all decisions were recorded only."
