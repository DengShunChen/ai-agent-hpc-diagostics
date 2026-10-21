import pytest

from hpc_agent.agents import SchedulerAgent
from hpc_agent.models import HypothesisStatus
from hpc_agent.tools.gateway import ScopedGateway, build_default_gateway
from tests.conftest import make_data_root

INCIDENT = {
    "incident_id": "INC-SCHED-001", "severity": "medium", "node": "sim-node-0001",
    "job_id": "SIM-JOB-1", "symptoms": ["job terminated unexpectedly"],
}


def job(**overrides):
    base = {"job_id": "SIM-JOB-1", "state": "FAILED", "exit_code": 1, "requested_walltime": "02:00:00",
            "elapsed": "00:30:00", "allocated_nodes": ["sim-node-0001"], "termination_reason": None}
    return base | overrides


def diagnose(tmp_path, scopes, test_data):
    adapter = make_data_root(tmp_path, INCIDENT, test_data)
    gateway = ScopedGateway(build_default_gateway(adapter), "scheduler-agent", scopes, [])
    return SchedulerAgent().diagnose(adapter.load_incident(INCIDENT["incident_id"]), gateway)


def test_walltime_reached(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, {"scheduler/job_status": job(exit_code=0, elapsed="1-00:00:00", requested_walltime="23:59:59")})
    assert "Job reached its walltime limit" in [c.cause for c in d.hypotheses]
    assert d.confidence == 0.5


@pytest.mark.parametrize("code,expected", [(139, "SIGSEGV"), (143, "SIGTERM"), (137, "SIGKILL"), (130, "signal 2")])
def test_signal_interpretation(tmp_path, scopes, code, expected):
    d = diagnose(tmp_path, scopes, {"scheduler/job_status": job(exit_code=code)})
    assert any(expected in c.cause for c in d.hypotheses)
    assert "storage, network and GPU state on the job's nodes at the failure time" in d.missing_information


def test_application_exit_code_points_at_application(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, {"scheduler/job_status": job(exit_code=2)})
    assert any("application's own error output" in c.cause for c in d.hypotheses)
    assert "review job stdout/stderr for application errors" in d.recommended_checks


def test_recorded_termination_reason_used(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, {"scheduler/job_status": job(termination_reason="NodeFail")})
    assert any(e.statement == "termination_reason=NodeFail" for e in d.evidence)
    assert not any("termination reason" in m for m in d.missing_information)


def test_unhealthy_node_outside_allocation_ignored(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, {
        "scheduler/job_status": job(),
        "scheduler/node_status": {"nodes": [{"node": "sim-node-0999", "state": "DOWN", "reason": None}]},
    })
    assert not any("sim-node-0999" in c.cause for c in d.possible_causes)
    assert "node:sim-node-0999" not in d.affected_components
    assert not any("sim-node-0999" in e.statement for e in d.evidence)


def test_drained_node_with_reason(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, {
        "scheduler/job_status": job(exit_code=137),
        "scheduler/node_status": {"nodes": [{"node": "sim-node-0001", "state": "DRAIN", "reason": "healthcheck"}]},
    })
    assert any(e.statement == "node sim-node-0001 state=DRAIN reason=healthcheck" for e in d.evidence)
    assert not any(m.startswith("reason node") for m in d.missing_information)


@pytest.mark.parametrize("field,value", [("exit_code", "137"), ("exit_code", True), ("elapsed", "soon")])
def test_malformed_job_fields_are_missing_not_guessed(tmp_path, scopes, field, value):
    d = diagnose(tmp_path, scopes, {"scheduler/job_status": job(**{field: value})})
    expected = "job exit code" if field == "exit_code" else "job elapsed time and walltime limit"
    assert expected in d.missing_information


def test_walltime_not_reached_is_ruled_out(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, {"scheduler/job_status": job()})
    [ruled_out] = [c for c in d.possible_causes if c.status is HypothesisStatus.RULED_OUT]
    assert ruled_out.cause == "Job reached its walltime limit"
    assert "walltime limit was not reached" in ruled_out.conflicting_evidence[0].statement


def test_components(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, {
        "scheduler/job_status": job(exit_code=137),
        "scheduler/node_status": {"nodes": [{"node": "sim-node-0001", "state": "DOWN", "reason": None}]},
    })
    assert d.affected_components == ["job:SIM-JOB-1", "node:sim-node-0001"]
