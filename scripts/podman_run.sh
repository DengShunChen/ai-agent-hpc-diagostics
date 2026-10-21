#!/usr/bin/env bash
# Run the API container with rootless Podman (Iteration 6).
#
#   HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json scripts/podman_run.sh
#
# Creates the token file first with:
#   .venv/bin/python -m hpc_agent.main create-operator-token ops.yourname \
#       --tokens-file ~/.config/hpc-agent/operators.json
#
# Security settings: non-root (host UID mapped via keep-id), read-only root filesystem,
# all capabilities dropped, no-new-privileges, resource limits, port published on
# 127.0.0.1 only, data volumes read-only, audit-data the only writable volume.
# The :Z option relabels the mounted host paths for SELinux (private to this container).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
IMAGE="${HPC_AGENT_IMAGE:-localhost/hpc-ai-agent:0.1.0}"
PORT="${HPC_AGENT_PORT:-8080}"
STATE="${HPC_AGENT_STATE_DIR:-$ROOT/audit-data}"
TOKENS="${HPC_AGENT_OPERATOR_TOKENS:?set HPC_AGENT_OPERATOR_TOKENS to the operator token-hash file}"
APP=/opt/app-root/app

case "$(realpath "$TOKENS")" in "$ROOT"/*) echo "token file must be outside the repository" >&2; exit 2;; esac
mkdir -p "$STATE"

exec podman run --rm --name "${HPC_AGENT_CONTAINER:-hpc-ai-agent}" \
    --userns=keep-id --user "$(id -u):$(id -g)" \
    --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
    --cap-drop=ALL --security-opt=no-new-privileges \
    --pids-limit=256 --memory=1g \
    -p "127.0.0.1:${PORT}:8080" \
    -v "$ROOT/fixtures:$APP/fixtures:ro,Z" \
    -v "$ROOT/test_data:$APP/test_data:ro,Z" \
    -v "$ROOT/knowledge:$APP/knowledge:ro,Z" \
    -v "$STATE:$APP/audit-data:rw,Z" \
    -v "$(realpath "$TOKENS"):/run/secrets/operators.json:ro,Z" \
    "$@" "$IMAGE"
