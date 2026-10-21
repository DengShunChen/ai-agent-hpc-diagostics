import math

import pytest
from pydantic import ValidationError

from hpc_agent.models import AgentDiagnosis, Domain, ProbableCause, RecommendedAction


def test_valid_diagnosis(reported):
    d = AgentDiagnosis(
        agent="storage-agent",
        domain="storage",
        finding="write failure reported while filesystem utilization reported high",
        evidence=[reported],
        possible_causes=[ProbableCause(cause="OST capacity or imbalance may contribute", domain="storage",
                                       supporting_evidence=[reported], confidence=0.3)],
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


def test_specialist_cannot_confirm_root_cause(tool_evidence):
    cause = ProbableCause(cause="OST full", status="confirmed", supporting_evidence=[tool_evidence])
    with pytest.raises(ValidationError, match="may not confirm"):
        AgentDiagnosis(agent="storage-agent", domain="storage", evidence=[tool_evidence], possible_causes=[cause])


def test_cause_evidence_must_belong_to_diagnosis(reported, tool_evidence):
    cause = ProbableCause(cause="OST full", supporting_evidence=[tool_evidence], confidence=0.5)
    with pytest.raises(ValidationError, match="part of the diagnosis evidence"):
        AgentDiagnosis(agent="storage-agent", domain="storage", evidence=[reported], possible_causes=[cause])


def test_ruled_out_cause_must_cite_conflict(reported):
    with pytest.raises(ValidationError, match="conflicting"):
        ProbableCause(cause="capacity", status="ruled_out", supporting_evidence=[reported])


def test_hypotheses_excludes_ruled_out(reported, tool_evidence):
    d = AgentDiagnosis(
        agent="storage-agent", domain="storage", evidence=[reported, tool_evidence],
        possible_causes=[
            ProbableCause(cause="a", supporting_evidence=[reported], confidence=0.2),
            ProbableCause(cause="b", status="ruled_out", supporting_evidence=[reported],
                          conflicting_evidence=[tool_evidence]),
        ],
    )
    assert [c.cause for c in d.hypotheses] == ["a"]


def test_knowledge_cannot_be_evidence():
    from hpc_agent.models import Evidence
    kb = Evidence(kind="knowledge", source="knowledge:knowledge/sop/SOP-STOR-001.md", statement="SOP")
    with pytest.raises(ValidationError, match="references"):
        AgentDiagnosis(agent="storage-agent", domain="storage", evidence=[kb])
    with pytest.raises(ValidationError, match="references"):
        ProbableCause(cause="x", supporting_evidence=[kb], confidence=0.1)
    AgentDiagnosis(agent="storage-agent", domain="storage", references=[kb])


def test_references_must_be_knowledge(reported):
    with pytest.raises(ValidationError, match="knowledge-base"):
        AgentDiagnosis(agent="storage-agent", domain="storage", references=[reported])


def test_observed_at_must_be_timezone_aware():
    from datetime import datetime

    from hpc_agent.models import Evidence
    with pytest.raises(ValidationError):
        Evidence(kind="tool_result", source="tool:x:y", statement="s", observed_at=datetime(2026, 1, 1))
