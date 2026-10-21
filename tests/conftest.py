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
