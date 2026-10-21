"""End-to-end Iteration 2 workflow runs on the three POC scenarios."""

import pytest

from hpc_agent.workflows.diagnostic_workflow import render_report, run_diagnosis


@pytest.fixture
def diagnose(adapter, gateway, scopes):
    return lambda incident_id: run_diagnosis(adapter.load_incident(incident_id), gateway, scopes)


def test_storage_001_refuses_unsupported_conclusion(diagnose):
    run = diagnose("POC-STORAGE-001")
    assert run.triage.classification == "storage"
    assert [d.agent for d in run.diagnoses] == ["storage-agent", "scheduler-agent"]

    storage = run.diagnoses[0]
    assert storage.possible_causes == [
        "Storage capacity or OST imbalance may be contributing to the write failure"
    ]
    assert storage.confidence < 0.5
    assert storage.recommended_actions == []

    statements = [e.statement for e in run.facts]
    assert statements == ["job failed while writing output", "filesystem utilization is high"]

    for item in ["actual filesystem utilization percentage", "individual OST utilization",
                 "filesystem health status",
                 "job termination details (state, exit code, termination reason)"]:
        assert item in run.missing_information
    for check in ["get_filesystem_usage()", "get_ost_usage()", "get_job_status()"]:
        assert check in run.recommended_checks

    text = " ".join([d.finding for d in run.diagnoses] + [c for d in run.diagnoses for c in d.possible_causes])
    assert "caused the failure" not in text.lower()
    assert "OST capacity caused" not in text


def test_scheduler_001_keeps_uncertainty(diagnose):
    run = diagnose("POC-SCHEDULER-001")
    assert run.triage.classification == "scheduler"
    [sched] = run.diagnoses
    assert any("SIGKILL" in c for c in sched.possible_causes)
    assert any("sim-node-0042" in c and "does not show" in c for c in sched.possible_causes)
    assert "none is confirmed" in sched.finding
    assert sched.confidence <= 0.3
    assert "reason node sim-node-0042 entered DRAIN" in run.missing_information
    assert "storage, network and GPU state on the job's nodes at the failure time" in run.missing_information
    assert any("walltime limit was not reached" in e.statement for e in sched.evidence)
    assert "Job reached its walltime limit" not in sched.possible_causes


def test_gpu_001_reports_unexamined_domain(diagnose):
    run = diagnose("POC-GPU-001")
    assert run.triage.classification == "gpu"
    assert run.unavailable_agents == ["gpu-agent"]
    assert "gpu-agent not available in this iteration: its domain was not examined" in run.missing_information
    assert not any("hardware" in c.lower() for d in run.diagnoses for c in d.possible_causes)
    assert not any(r.tool == "get_gpu_health" for r in run.tools_invoked)


@pytest.mark.parametrize("incident_id", ["POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001"])
def test_report_separates_sections(diagnose, incident_id):
    report = render_report(diagnose(incident_id))
    for header in ["ROOT CAUSE: NOT DETERMINED", "FACTS", "HYPOTHESES (unconfirmed)", "MISSING INFORMATION",
                   "RECOMMENDED CHECKS", "RECOMMENDED ACTIONS (proposals only", "TOOLS INVOKED"]:
        assert header in report


def test_runs_are_deterministic(diagnose):
    a, b = diagnose("POC-SCHEDULER-001"), diagnose("POC-SCHEDULER-001")
    assert a.run_id != b.run_id
    assert a.diagnoses == b.diagnoses and a.triage == b.triage
