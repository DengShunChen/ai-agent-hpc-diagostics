from datetime import UTC, datetime
from pathlib import Path

import pytest

from hpc_agent.data_adapter import ReadOnlyDataAdapter
from hpc_agent.models import Evidence, EvidenceKind
from hpc_agent.tools.gateway import build_default_gateway

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def adapter() -> ReadOnlyDataAdapter:
    return ReadOnlyDataAdapter(PROJECT_ROOT)


@pytest.fixture
def gateway(adapter):
    return build_default_gateway(adapter)


@pytest.fixture
def reported() -> Evidence:
    return Evidence(
        kind=EvidenceKind.INCIDENT_REPORT,
        source="POC-STORAGE-001:symptoms[0]",
        statement="job failed while writing output",
    )


@pytest.fixture
def tool_evidence() -> Evidence:
    return Evidence(
        kind=EvidenceKind.TOOL_RESULT,
        source="tool:get_ost_usage",
        statement="OST0003 reported 99% used",
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def make_data_root(tmp_path: Path, incident: dict, test_data: dict | None = None) -> ReadOnlyDataAdapter:
    """Build an isolated data root holding one incident and optional saved tool outputs.

    test_data maps "domain/dataset" (e.g. "storage/ost_usage") to the JSON payload.
    """
    import json

    (tmp_path / "fixtures" / "incidents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fixtures" / "incidents" / f"{incident['incident_id']}.json").write_text(json.dumps(incident))
    for key, payload in (test_data or {}).items():
        domain, dataset = key.split("/")
        path = tmp_path / "test_data" / domain / incident["incident_id"] / f"{dataset}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    return ReadOnlyDataAdapter(tmp_path)


@pytest.fixture
def scopes():
    from hpc_agent.config import load_agent_tool_scopes

    return load_agent_tool_scopes(PROJECT_ROOT)
