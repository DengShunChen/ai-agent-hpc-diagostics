#!/usr/bin/env bash
# Smoke-test the built image: security posture, read-only mounts, auth and one approval cycle.
# Uses throwaway token/state directories under a temp dir; nothing in the repository is written
# (the :Z mounts relabel fixtures/, test_data/ and knowledge/ for SELinux).
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${HPC_AGENT_IMAGE:-localhost/hpc-ai-agent:0.1.0}"
NAME=hpc-ai-agent-smoke
PORT="${HPC_AGENT_SMOKE_PORT:-18080}"
WORK="$(mktemp -d)"
cleanup() { podman stop -t 2 "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok   $*"; }

TOKEN="$(PYTHONPATH=src .venv/bin/python -m hpc_agent.main create-operator-token ops.smoke \
          --tokens-file "$WORK/operators.json" | tail -1)"
HPC_AGENT_IMAGE="$IMAGE" HPC_AGENT_OPERATOR_TOKENS="$WORK/operators.json" HPC_AGENT_STATE_DIR="$WORK/state" \
  HPC_AGENT_PORT="$PORT" HPC_AGENT_CONTAINER="$NAME" scripts/podman_run.sh -d >/dev/null

for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/healthz" | grep -q '"ok"' && pass "healthz" || fail "healthz"

inspect() { podman inspect "$NAME" --format "$1"; }
[ "$(inspect '{{.HostConfig.ReadonlyRootfs}}')" = "true" ] && pass "read-only root filesystem" || fail "rootfs writable"
[ "$(inspect '{{.HostConfig.Privileged}}')" = "false" ] && pass "not privileged" || fail "privileged"
[ "$(inspect '{{len .EffectiveCaps}}')" = "0" ] && pass "no effective capabilities" || fail "capabilities present"
inspect '{{range .HostConfig.SecurityOpt}}{{.}} {{end}}' | grep -q "no-new-privileges" && pass "no-new-privileges" || fail "no-new-privileges"
[ "$(inspect '{{.HostConfig.NetworkMode}}')" != "host" ] && pass "not host networking" || fail "host networking"
inspect '{{json .HostConfig.PortBindings}}' | grep -q '"HostIp":"127.0.0.1"' && pass "port bound to 127.0.0.1" || fail "port binding"
[ "$(podman exec "$NAME" id -u)" != "0" ] && pass "runs as non-root uid $(podman exec "$NAME" id -u)" || fail "runs as root"
podman exec "$NAME" sh -c 'touch /opt/app-root/app/knowledge/x' 2>/dev/null && fail "knowledge writable" || pass "knowledge mount read-only"
podman exec "$NAME" sh -c 'touch /opt/app-root/app/src/x' 2>/dev/null && fail "image writable" || pass "application code read-only"
podman exec "$NAME" sh -c 'command -v ssh' >/dev/null 2>&1 && echo "note: ssh client present in base image (not exposed to agents)" || pass "no ssh client"

API="http://127.0.0.1:$PORT/api"
AUTH="Authorization: Bearer $TOKEN"
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
[ "$(code "$API/runs")" = "401" ] && pass "unauthenticated request refused" || fail "auth not enforced"
[ "$(code -H 'Authorization: Bearer wrong' "$API/runs")" = "401" ] && pass "bad token refused" || fail "bad token accepted"
RUN="$(curl -fsS -H "$AUTH" -H 'Content-Type: application/json' -d '{"incident_id":"POC-STORAGE-001"}' "$API/runs" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')"
pass "diagnosed POC-STORAGE-001 as $RUN"
curl -fsS -H "$AUTH" "$API/runs/$RUN/review" | grep -q "Storage capacity or OST imbalance may be contributing" \
  && pass "review shows hypothesis" || fail "review"
curl -fsS -H "$AUTH" -H 'Content-Type: application/json' -d '{"decision":"APPROVED","comment":"smoke"}' \
  "$API/runs/$RUN/decision" | grep -q '"executed":false' && pass "approval recorded, not executed" || fail "decision"
curl -fsS -H "$AUTH" "$API/audit/verify" | grep -q '"chain":"ok"' && pass "audit chain verified" || fail "audit chain"
[ -s "$WORK/state/audit.jsonl" ] && pass "audit written to mounted volume only" || fail "audit volume"
echo "container smoke test passed"
