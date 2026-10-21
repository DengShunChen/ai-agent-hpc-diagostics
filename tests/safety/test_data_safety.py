"""Credential detection, read-only confinement and fixture hygiene."""

import json
import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from hpc_agent.data_adapter import DataAccessError, ReadOnlyDataAdapter
from hpc_agent.models import AuditRecord, Incident
from hpc_agent.safety.secrets import SENSITIVE_NOTICE, SensitiveDataError, find_credential_like
from tests.conftest import PROJECT_ROOT

FAKE_SECRET = "hunter2-not-a-real-value"


def _write_incident(root, incident_id, payload):
    path = root / "fixtures" / "incidents" / f"{incident_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return ReadOnlyDataAdapter(root)


@pytest.mark.parametrize(
    "extra",
    [
        {"password": FAKE_SECRET},
        {"api_key": FAKE_SECRET},
        {"meta": {"ssh_private_key": FAKE_SECRET}},
        {"logs": [f"login ok password={FAKE_SECRET}"]},
        {"logs": ["-----BEGIN OPENSSH PRIVATE KEY-----"]},
        {"operator_notes": ["Authorization: Bearer abcdefghijklmnop"]},
    ],
)
def test_fixture_with_credential_like_content_rejected(tmp_path, extra):
    payload = {"incident_id": "POC-BAD-001", "severity": "high", **extra}
    adapter = _write_incident(tmp_path, "POC-BAD-001", payload)
    with pytest.raises(SensitiveDataError) as excinfo:
        adapter.load_incident("POC-BAD-001")
    message = str(excinfo.value)
    assert SENSITIVE_NOTICE in message
    assert FAKE_SECRET not in message  # never echo the value


def test_incident_model_rejects_credential_fields():
    with pytest.raises(ValidationError):
        Incident(incident_id="INC-001", severity="high", token=FAKE_SECRET)


def test_audit_record_refuses_credentials():
    with pytest.raises(ValidationError):
        AuditRecord(
            run_id="r", incident_id="INC-001", start_time=datetime.now(UTC),
            operator_comment=f"api_key={FAKE_SECRET}",
        )


def test_fixture_id_must_match_filename(tmp_path):
    adapter = _write_incident(tmp_path, "POC-A-001", {"incident_id": "POC-B-001", "severity": "low"})
    with pytest.raises(DataAccessError):
        adapter.load_incident("POC-A-001")


@pytest.mark.parametrize("segments", [("..", "etc"), ("/etc", "passwd"), ("src", "hpc_agent"),
                                      ("audit-data",), ("fixtures", "../../x"), ("fixtures", "")])
def test_reads_confined_to_data_roots(adapter, segments):
    with pytest.raises(DataAccessError):
        adapter.read_json(*segments)


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (tmp_path / "data" / "fixtures").mkdir(parents=True)
    (tmp_path / "data" / "fixtures" / "link.json").symlink_to(outside)
    with pytest.raises(DataAccessError):
        ReadOnlyDataAdapter(tmp_path / "data").read_json("fixtures", "link.json")


def _repo_data_files():
    for d in ("fixtures", "test_data", "knowledge", "evaluation"):
        yield from (p for p in (PROJECT_ROOT / d).rglob("*") if p.is_file() and p.name != ".gitkeep")


@pytest.mark.parametrize("path", list(_repo_data_files()), ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_repo_data_is_clean(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        assert find_credential_like(json.loads(text)) == []
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text), "IPv4 address in data"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "email address in data"


@pytest.mark.parametrize("incident_id", ["POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001"])
def test_fixture_identifiers_are_synthetic(adapter, incident_id):
    incident = adapter.load_incident(incident_id)
    if incident.node:
        assert incident.node.startswith("sim-")
    if incident.job_id:
        assert incident.job_id.startswith("SIM-")
