# Example site export (synthetic)

A **synthetic** example of the CSV export read by `hpc-agent import-export`. The column
layout is documented in `src/hpc_agent/ingest/csv_export.py`, and the target shapes are in
`src/hpc_agent/ingest/contract.py`.

The three incidents mirror the POC scenarios: a drained node with a SIGKILLed job, a GPU
XID 13, and a network link down. Real exports must never be placed in this repository.
Import them into a data root outside it:

    export HPC_AGENT_PSEUDONYM_KEY=...   # at least 32 characters, kept outside the repo
    PYTHONPATH=src .venv/bin/python -m hpc_agent.main import-export examples/site-export \
        --output ~/hpc-agent-data --dry-run
