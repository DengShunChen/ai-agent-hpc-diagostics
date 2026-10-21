import pytest
from pydantic import ValidationError

from hpc_agent.models import ApprovalRecord, ApprovalStatus as S
from hpc_agent.safety.approval import ALLOWED_TRANSITIONS, InvalidTransitionError, transition


def at(status, operator=None):
    return ApprovalRecord(run_id="run-1", incident_id="INC-001", status=status, operator=operator)


@pytest.mark.parametrize(
    "start,target",
    [
        (S.DRAFT, S.APPROVED),  # skip diagnosis + review
        (S.DIAGNOSIS_COMPLETE, S.APPROVED),  # skip awaiting approval
        (S.DRAFT, S.CLOSED),
        (S.REJECTED, S.APPROVED),
        (S.REJECTED, S.CLOSED),
        (S.AWAITING_HUMAN_APPROVAL, S.CLOSED),
        (S.CLOSED, S.DRAFT),
        (S.APPROVED, S.APPROVED),
    ],
)
def test_invalid_transitions_rejected(start, target):
    operator = "ops.wang" if start in (S.APPROVED, S.REJECTED) else None
    with pytest.raises(InvalidTransitionError):
        transition(at(start, operator), target, operator="ops.wang")


def test_every_status_has_transition_rule():
    assert set(ALLOWED_TRANSITIONS) == set(S)
    assert ALLOWED_TRANSITIONS[S.CLOSED] == frozenset()


@pytest.mark.parametrize("decision", [S.APPROVED, S.REJECTED, S.REVISION_REQUESTED])
def test_decision_requires_operator(decision):
    with pytest.raises(ValidationError):
        transition(at(S.AWAITING_HUMAN_APPROVAL), decision)


@pytest.mark.parametrize(
    "operator",
    ["rca-coordinator", "storage-agent", "triage_agent", "system", "AI", "llm", "claude",
     "approval-bot", "", "x", "ops wang", "ops;rm"],
)
def test_ai_or_invalid_identities_cannot_approve(operator):
    with pytest.raises(ValidationError):
        transition(at(S.AWAITING_HUMAN_APPROVAL), S.APPROVED, operator=operator)


def test_approved_record_cannot_be_mutated():
    r = transition(at(S.AWAITING_HUMAN_APPROVAL), S.APPROVED, operator="ops.wang")
    with pytest.raises(ValidationError):
        r.operator = "storage-agent"
