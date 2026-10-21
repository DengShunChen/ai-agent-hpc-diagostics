"""Human-in-the-loop approval state machine.

DRAFT -> DIAGNOSIS_COMPLETE -> AWAITING_HUMAN_APPROVAL
AWAITING_HUMAN_APPROVAL -> APPROVED | REJECTED | REVISION_REQUESTED  (human only)
APPROVED -> CLOSED;  REJECTED | REVISION_REQUESTED -> REVISE -> DRAFT

Transitions only produce new ApprovalRecords. Nothing here executes an action.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

from hpc_agent.models.approval import ApprovalRecord, ApprovalStatus as S

ALLOWED_TRANSITIONS = MappingProxyType(
    {
        S.DRAFT: frozenset({S.DIAGNOSIS_COMPLETE}),
        S.DIAGNOSIS_COMPLETE: frozenset({S.AWAITING_HUMAN_APPROVAL}),
        S.AWAITING_HUMAN_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.REVISION_REQUESTED}),
        S.APPROVED: frozenset({S.CLOSED}),
        S.REJECTED: frozenset({S.REVISE}),
        S.REVISION_REQUESTED: frozenset({S.REVISE}),
        S.REVISE: frozenset({S.DRAFT}),
        S.CLOSED: frozenset(),
    }
)


class InvalidTransitionError(ValueError):
    pass


def transition(
    current: ApprovalRecord,
    new_status: S,
    operator: str | None = None,
    operator_comment: str = "",
) -> ApprovalRecord:
    """Return the next record. Raises on a disallowed transition or a non-human decision."""
    if new_status not in ALLOWED_TRANSITIONS[current.status]:
        raise InvalidTransitionError(f"{current.status} -> {new_status} is not allowed")
    # ApprovalRecord validation enforces a human operator for decision statuses.
    return ApprovalRecord(
        run_id=current.run_id,
        incident_id=current.incident_id,
        status=new_status,
        operator=operator,
        operator_comment=operator_comment,
        timestamp=datetime.now(UTC),
    )
