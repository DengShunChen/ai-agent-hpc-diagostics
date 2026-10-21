import math

import pytest
from pydantic import ValidationError

from hpc_agent.models import AgentDiagnosis, Domain, RecommendedAction


def test_valid_diagnosis(reported):
    d = AgentDiagnosis(
        agent="storage-agent",
        domain="storage",
        finding="write failure reported while filesystem utilization reported high",
        evidence=[reported],
        possible_causes=["OST capacity or imbalance may be contributing"],
        recommended_checks=["get_ost_usage"],
        missing_information=["individual OST utilization"],
        confidence=0.3,
    )
    assert d.domain is Domain.STORAGE
    assert d.human_approval_required is True


def test_empty_diagnosis_allowed_when_nothing_is_claimed():
    d = AgentDiagnosis(agent="gpu-agent", domain="gpu", missing_information=["gpu health"])
    assert d.confidence == 0.0


@pytest.mark.parametrize("confidence", [1.01, 2, -0.01, -1, math.nan, math.inf])
def test_confidence_out_of_range_rejected(reported, confidence):
    with pytest.raises(ValidationError):
        AgentDiagnosis(
            agent="storage-agent", domain="storage", evidence=[reported], confidence=confidence
        )


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_confidence_bounds_accepted(reported, confidence):
    AgentDiagnosis(agent="storage-agent", domain="storage", evidence=[reported], confidence=confidence)


def test_finding_without_evidence_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        AgentDiagnosis(agent="storage-agent", domain="storage", finding="OST full")


def test_confidence_cannot_replace_evidence():
    with pytest.raises(ValidationError, match="evidence"):
        AgentDiagnosis(agent="storage-agent", domain="storage", confidence=0.9)


def test_human_approval_cannot_be_disabled():
    with pytest.raises(ValidationError):
        AgentDiagnosis(agent="storage-agent", domain="storage", human_approval_required=False)


@pytest.mark.parametrize("agent", ["storage", "Storage-Agent", "", "rm -rf"])
def test_invalid_agent_name_rejected(agent):
    with pytest.raises(ValidationError):
        AgentDiagnosis(agent=agent, domain="storage")


def test_unknown_domain_rejected():
    with pytest.raises(ValidationError):
        AgentDiagnosis(agent="storage-agent", domain="database")


def test_recommended_action_requires_reason_and_component():
    with pytest.raises(ValidationError):
        RecommendedAction(action="drain node", reason="", risk="high", affected_component="sim-node")
    with pytest.raises(ValidationError):
        RecommendedAction(action="drain node", reason="x", risk="extreme", affected_component="n")
