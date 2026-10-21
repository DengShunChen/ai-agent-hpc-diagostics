import pytest

from hpc_agent.agents import StorageAgent
from hpc_agent.models import EvidenceKind
from hpc_agent.tools.gateway import ScopedGateway, build_default_gateway
from tests.conftest import make_data_root

WRITE_FAILURE = {
    "incident_id": "INC-STOR-001",
    "severity": "high",
    "symptoms": ["job failed while writing output", "filesystem utilization is high"],
}


def diagnose(tmp_path, scopes, test_data=None, incident=WRITE_FAILURE):
    adapter = make_data_root(tmp_path, incident, test_data)
    log = []
    gateway = ScopedGateway(build_default_gateway(adapter), "storage-agent", scopes, log)
    return StorageAgent().diagnose(adapter.load_incident(incident["incident_id"]), gateway), log


def osts(*pcts, state="active"):
    return {"osts": [{"ost": f"OST{i:04d}", "used_pct": p, "state": state} for i, p in enumerate(pcts)]}


def test_no_data_gives_hypothesis_only(tmp_path, scopes):
    d, log = diagnose(tmp_path, scopes)
    assert d.possible_causes == ["Storage capacity or OST imbalance may be contributing to the write failure"]
    assert d.confidence == 0.25
    assert d.recommended_actions == []
    assert all(e.kind is EvidenceKind.INCIDENT_REPORT for e in d.evidence)
    assert [r.result_status for r in log] == ["no_data", "no_data"]


def test_full_ost_supports_capacity_hypothesis(tmp_path, scopes):
    d, _ = diagnose(tmp_path, scopes, {
        "storage/filesystem_usage": {"filesystem": "simfs01", "used_pct": 82, "health": "healthy"},
        "storage/ost_usage": osts(80, 81, 98.5),
    })
    assert d.possible_causes[0] == "Full filesystem or OST capacity may have caused the write failure"
    assert d.confidence == 0.6
    assert "'No space left on device' (ENOSPC) or quota error in the job output" in d.missing_information
    [action] = d.recommended_actions
    assert action.affected_component == "OST0002"
    assert action.supporting_evidence and all(e.kind is EvidenceKind.TOOL_RESULT for e in action.supporting_evidence)
    assert "none is confirmed" in d.finding


def test_measured_not_full_overrides_reported_claim(tmp_path, scopes):
    d, _ = diagnose(tmp_path, scopes, {
        "storage/filesystem_usage": {"filesystem": "simfs01", "used_pct": 61, "health": "healthy"},
        "storage/ost_usage": osts(58, 60, 63),
    })
    assert d.possible_causes == []
    assert d.confidence == 0.0
    assert "not supported" in d.finding
    assert d.missing_information == [] and d.recommended_checks == []


def test_imbalance_detected(tmp_path, scopes):
    d, _ = diagnose(tmp_path, scopes, {"storage/ost_usage": osts(40, 45, 88)})
    assert any("OST imbalance" in c for c in d.possible_causes)
    assert "actual filesystem utilization percentage" in d.missing_information


def test_degraded_health(tmp_path, scopes):
    d, _ = diagnose(tmp_path, scopes, {
        "storage/filesystem_usage": {"filesystem": "simfs01", "used_pct": 50, "health": "degraded"},
        "storage/ost_usage": osts(50, 52, state="inactive"),
    })
    assert "Degraded filesystem or OST health may be causing I/O failures" in d.possible_causes
    assert d.confidence == 0.5


@pytest.mark.parametrize("bad", ["97", True, None, [97]])
def test_non_numeric_utilization_is_missing_not_guessed(tmp_path, scopes, bad):
    d, _ = diagnose(tmp_path, scopes, {
        "storage/filesystem_usage": {"filesystem": "simfs01", "used_pct": bad, "health": "healthy"},
        "storage/ost_usage": {"osts": [{"ost": "OST0000", "used_pct": bad}]},
    })
    assert "actual filesystem utilization percentage" in d.missing_information
    assert "individual OST utilization" in d.missing_information
    assert not any("used" in e.statement for e in d.evidence if e.kind is EvidenceKind.TOOL_RESULT)


def test_unrelated_incident_has_no_finding(tmp_path, scopes):
    incident = {"incident_id": "INC-GPU-009", "severity": "low", "symptoms": ["CUDA error"]}
    d, _ = diagnose(tmp_path, scopes, incident=incident)
    assert d.finding == "" and d.evidence == [] and d.confidence == 0.0
