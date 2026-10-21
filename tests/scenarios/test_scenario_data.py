"""Scenario data checks for Iteration 1.

Agents do not exist yet. These tests pin down what evidence each scenario does and
does not contain, so later iterations must reach their conclusions from that evidence.
"""

import pytest
from pydantic import ValidationError

from hpc_agent.models import Evidence, EvidenceKind, ProbableCause, RCAResult
from hpc_agent.tools.gateway import ToolStatus


# Scenario 01 — FEFS/Lustre write failure: not enough evidence for a root cause.
@pytest.mark.parametrize("tool", ["get_filesystem_usage", "get_ost_usage", "get_job_status"])
def test_storage_001_recommended_checks_return_no_data(gateway, tool):
    result = gateway.invoke(tool, incident_id="POC-STORAGE-001")
    assert result.status is ToolStatus.NO_DATA
    assert result.data is None and result.source is None


def test_storage_001_cannot_confirm_ost_capacity(adapter):
    incident = adapter.load_incident("POC-STORAGE-001")
    facts = [
        Evidence(kind=EvidenceKind.INCIDENT_REPORT, source=f"{incident.incident_id}:symptoms[{i}]",
                 statement=s)
        for i, s in enumerate(incident.symptoms)
    ]
    with pytest.raises(ValidationError):
        ProbableCause(cause="OST capacity caused the failure", status="confirmed",
                      supporting_evidence=facts)
    # The acceptable shape: a hypothesis plus missing information and checks.
    rca = RCAResult(
        incident_id=incident.incident_id,
        classification="storage",
        affected_domains=["storage", "scheduler"],
        facts=facts,
        probable_causes=[ProbableCause(
            cause="Storage capacity or OST imbalance may be contributing to the write failure",
            supporting_evidence=facts, confidence=0.3)],
        missing_information=["actual filesystem utilization percentage",
                             "individual OST utilization", "filesystem health status",
                             "job termination details"],
        recommended_checks=["get_filesystem_usage", "get_ost_usage", "get_job_status"],
        confidence=0.3,
    )
    assert rca.confirmed_root_cause is None


# Scenario 02 — scheduler job failure: partial, ambiguous evidence.
def test_scheduler_001_evidence_is_partial(gateway):
    job = gateway.invoke("get_job_status", incident_id="POC-SCHEDULER-001")
    nodes = gateway.invoke("get_node_status", incident_id="POC-SCHEDULER-001")
    assert job.status is nodes.status is ToolStatus.OK
    assert job.data["state"] == "FAILED" and job.data["termination_reason"] is None
    assert all(n["reason"] is None for n in nodes.data["nodes"])
    # No cross-domain data saved: storage/network/GPU checks must stay open questions.
    assert gateway.invoke("get_network_status", incident_id="POC-SCHEDULER-001").status is ToolStatus.NO_DATA


# Scenario 03 — GPU node error: saved DCGM-style data is not enough to claim hardware failure.
def test_gpu_001_saved_data_is_inconclusive(gateway):
    result = gateway.invoke("get_gpu_health", incident_id="POC-GPU-001")
    assert result.status is ToolStatus.OK
    assert result.data["_synthetic"] is True
    gpu3 = next(g for g in result.data["gpus"] if g["index"] == 3)
    assert gpu3["health"] == "warn"
    for field in ("temperature_c", "power_w", "ecc_dbe_volatile", "pcie_replay_count", "nvlink_errors"):
        assert gpu3[field] is None
