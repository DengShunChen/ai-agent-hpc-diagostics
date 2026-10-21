#!/usr/bin/env bash
# Run the test suite from the project-local virtual environment.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m pytest "$@"
