#!/usr/bin/env bash
# Run the API natively (development). Listens on 127.0.0.1 only.
#   HPC_AGENT_OPERATOR_TOKENS=~/.config/hpc-agent/operators.json scripts/run_api.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=src exec .venv/bin/python -m hpc_agent.main serve --host 127.0.0.1 --port "${HPC_AGENT_PORT:-8080}"
