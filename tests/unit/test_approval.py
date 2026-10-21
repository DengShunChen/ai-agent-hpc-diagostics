from datetime import datetime

import pytest
from pydantic import ValidationError

from hpc_agent.models import ApprovalRecord, ApprovalStatus as S
from hpc_agent.safety.approval import transition


def draft() -> ApprovalRecord:
    return ApprovalRecord(run_id="run-0001", incident_id="POC-STORAGE-001", status=S.DRAFT)


def test_happy_path_to_closed():
    r = transition(draft(), S.DIAGNOSIS_COMPLETE)
    r = transition(r, S.AWAITING_HUMAN_APPROVAL)
    r = transition(r, S.APPROVED, operator="ops.wang", operator_comment="checked evidence")
    assert r.status is S.APPROVED and r.operator == "ops.wang"
    r = transition(r, S.CLOSED)
    assert r.status is S.CLOSED


@pytest.mark.parametrize("decision", [S.REJECTED, S.REVISION_REQUESTED])
def test_reject_or_revise_loops_back_to_draft(decision):
    r = transition(transition(draft(), S.DIAGNOSIS_COMPLETE), S.AWAITING_HUMAN_APPROVAL)
    r = transition(r, decision, operator="ops.lee", operator_comment="need OST data")
    r = transition(r, S.REVISE)
    assert transition(r, S.DRAFT).status is S.DRAFT


def test_record_fields():
    r = draft()
    assert r.timestamp.tzinfo is not None
    assert r.operator is None and r.operator_comment == ""


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        ApprovalRecord(run_id="r", incident_id="INC-001", status=S.DRAFT, timestamp=datetime(2026, 1, 1))


def test_unknown_status_rejected():
    with pytest.raises(ValidationError):
        ApprovalRecord(run_id="r", incident_id="INC-001", status="AUTO_APPROVED")
