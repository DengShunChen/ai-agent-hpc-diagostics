from datetime import timedelta

import pytest
from pydantic import ValidationError

from hpc_agent.models import AuditRecord, ToolInvocationRecord


def test_valid_audit_record(now):
    record = AuditRecord(
        run_id="run-0001",
        incident_id="POC-STORAGE-001",
        start_time=now,
        end_time=now + timedelta(seconds=2.5),
        agents_invoked=["triage-agent", "storage-agent"],
        tools_invoked=[
            ToolInvocationRecord(
                tool="get_ost_usage", agent="storage-agent", result_status="no_data", duration_ms=1.2
            )
        ],
        evidence_used=["POC-STORAGE-001:symptoms[0]"],
        diagnosis="insufficient evidence",
        recommendations=["get_ost_usage"],
        approval_status="AWAITING_HUMAN_APPROVAL",
    )
    assert record.duration_seconds == 2.5


def test_audit_record_is_immutable(now):
    record = AuditRecord(run_id="r", incident_id="INC-001", start_time=now)
    with pytest.raises(ValidationError):
        record.diagnosis = "changed"


def test_end_before_start_rejected(now):
    with pytest.raises(ValidationError):
        AuditRecord(run_id="r", incident_id="INC-001", start_time=now, end_time=now - timedelta(1))


def test_missing_ids_rejected(now):
    with pytest.raises(ValidationError):
        AuditRecord(incident_id="INC-001", start_time=now)
    with pytest.raises(ValidationError):
        AuditRecord(run_id="r", start_time=now)


def test_negative_duration_rejected():
    with pytest.raises(ValidationError):
        ToolInvocationRecord(tool="get_ost_usage", result_status="ok", duration_ms=-1)
