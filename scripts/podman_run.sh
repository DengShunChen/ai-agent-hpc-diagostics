#!/usr/bin/env bash
# Run the API container with rootless Podman (Iteration 6).
#
#   HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json scripts/podman_run.sh
#
# Creates the token file first with:
#   PYTHONPATH=src .venv/bin/python -m hpc_agent.main create-operator-token ops.yourname \
#       --tokens-file ~/.config/hpc-agent/operators.json
#
# Security settings: non-root (host UID mapped via keep-id), read-only root filesystem,
# all capabilities dropped, no-new-privileges, resource limits, port published on
# 127.0.0.1 only, data volumes read-only, audit-data the only writable volume.
# The :Z option relabels the mounted host paths for SELinux (private to this container).
#
# Pilot data (imported with `hpc-agent import-export`, outside the repository) and site profile:
#   HPC_AGENT_DATA_DIR=~/hpc-pilot/data HPC_AGENT_SITE=<site-id> \
#   HPC_AGENT_STATE_DIR=~/hpc-pilot/audit-data scripts/podman_run.sh
# The data dir must hold fixtures/, test_data/ and knowledge/. The site profile is read from
# config/sites/ inside the image, so add it before building.
#
# Optional local LLM explainer (Ollama on this host, listening on 127.0.0.1:11434):
#   MODEL_PROVIDER=ollama MODEL_NAME=qwen2.5:3b scripts/podman_run.sh
# This adds a pasta mapping so the container can reach the host's loopback as
# host.containers.internal. Note: that mapping exposes ALL host-loopback services to the
# container, so it is only added when MODEL_PROVIDER is set.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
IMAGE="${HPC_AGENT_IMAGE:-localhost/hpc-ai-agent:0.1.0}"
PORT="${HPC_AGENT_PORT:-8080}"
STATE="${HPC_AGENT_STATE_DIR:-$ROOT/audit-data}"
DATA="$(realpath "${HPC_AGENT_DATA_DIR:-$ROOT}")"
SITE="${HPC_AGENT_SITE:-default}"
TOKENS="${HPC_AGENT_OPERATOR_TOKENS:?set HPC_AGENT_OPERATOR_TOKENS to the operator token-hash file}"
APP=/opt/app-root/app

case "$(realpath "$TOKENS")" in "$ROOT"/*) echo "token file must be outside the repository" >&2; exit 2;; esac
for d in fixtures test_data knowledge; do
    [ -d "$DATA/$d" ] || { echo "data dir $DATA has no $d/ directory" >&2; exit 2; }
done
[ -f "$ROOT/config/sites/$SITE.yaml" ] || { echo "no site profile config/sites/$SITE.yaml" >&2; exit 2; }
mkdir -p "$STATE"

LLM_ARGS=()
if [ -n "${MODEL_PROVIDER:-}" ]; then
    LLM_ARGS=(--network "pasta:--map-host-loopback,169.254.1.2"
              --add-host "host.containers.internal:169.254.1.2"
              -e "MODEL_PROVIDER=$MODEL_PROVIDER"
              -e "MODEL_NAME=${MODEL_NAME:-qwen2.5:3b}"
              -e "MODEL_ENDPOINT=http://host.containers.internal:11434"
              -e "HPC_AGENT_LLM_TIMEOUT=${HPC_AGENT_LLM_TIMEOUT:-300}")
fi

exec podman run --rm --name "${HPC_AGENT_CONTAINER:-hpc-ai-agent}" \
    --userns=keep-id --user "$(id -u):$(id -g)" \
    --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
    --cap-drop=ALL --security-opt=no-new-privileges \
    --pids-limit=256 --memory=1g \
    -p "127.0.0.1:${PORT}:8080" \
    -e "HPC_AGENT_SITE=$SITE" \
    -v "$DATA/fixtures:$APP/fixtures:ro,Z" \
    -v "$DATA/test_data:$APP/test_data:ro,Z" \
    -v "$DATA/knowledge:$APP/knowledge:ro,Z" \
    -v "$STATE:$APP/audit-data:rw,Z" \
    -v "$(realpath "$TOKENS"):/run/secrets/operators.json:ro,Z" \
    "${LLM_ARGS[@]}" "$@" "$IMAGE"
