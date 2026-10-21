import pytest
from pydantic import ValidationError

from hpc_agent.models import HypothesisStatus, ProbableCause, RCAResult


def test_hypothesis_only_rca(reported):
    rca = RCAResult(
        incident_id="POC-STORAGE-001",
        classification="storage",
        affected_domains=["storage", "scheduler"],
        facts=[reported],
        probable_causes=[
            ProbableCause(
                cause="OST capacity or imbalance may contribute",
                supporting_evidence=[reported],
                confidence=0.3,
            )
        ],
        missing_information=["individual OST utilization"],
        recommended_checks=["get_ost_usage"],
        confidence=0.3,
    )
    assert rca.confirmed_root_cause is None
    assert rca.human_approval_required is True


def test_cannot_confirm_from_reported_symptoms_only(reported):
    with pytest.raises(ValidationError, match="tool-derived"):
        ProbableCause(
            cause="OST capacity caused the failure",
            status=HypothesisStatus.CONFIRMED,
            supporting_evidence=[reported],
        )


def test_cannot_confirm_without_any_evidence():
    with pytest.raises(ValidationError):
        ProbableCause(cause="GPU hardware failure", status="confirmed")


def test_cannot_confirm_with_conflicting_evidence(reported, tool_evidence):
    with pytest.raises(ValidationError, match="conflicting"):
        ProbableCause(
            cause="OST full",
            status="confirmed",
            supporting_evidence=[tool_evidence],
            conflicting_evidence=[reported],
        )


def test_confirmed_with_tool_evidence(tool_evidence):
    cause = ProbableCause(
        cause="OST full", status="confirmed", supporting_evidence=[tool_evidence], confidence=0.8
    )
    rca = RCAResult(
        incident_id="INC-001", classification="storage", probable_causes=[cause],
        supporting_evidence=[tool_evidence], confidence=0.8,
    )
    assert rca.confirmed_root_cause is cause


def test_only_one_confirmed_cause(tool_evidence):
    causes = [
        ProbableCause(cause=c, status="confirmed", supporting_evidence=[tool_evidence])
        for c in ("a", "b")
    ]
    with pytest.raises(ValidationError, match="at most one"):
        RCAResult(incident_id="INC-001", classification="storage", probable_causes=causes)


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_rca_confidence_range(confidence, reported):
    with pytest.raises(ValidationError):
        RCAResult(incident_id="INC-001", classification="storage", facts=[reported], confidence=confidence)


def test_rca_confidence_needs_evidence():
    with pytest.raises(ValidationError):
        RCAResult(incident_id="INC-001", classification="unknown", confidence=0.5)


def test_rca_missing_incident_id_rejected():
    with pytest.raises(ValidationError):
        RCAResult(classification="storage")


def test_rca_human_approval_cannot_be_disabled():
    with pytest.raises(ValidationError):
        RCAResult(incident_id="INC-001", classification="storage", human_approval_required=False)
