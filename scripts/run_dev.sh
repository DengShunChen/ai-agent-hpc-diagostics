#!/usr/bin/env bash
# Iteration 1: validate fixtures and list allowlisted tools. No server, no network access.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=src exec .venv/bin/python -m hpc_agent.main "$@"
