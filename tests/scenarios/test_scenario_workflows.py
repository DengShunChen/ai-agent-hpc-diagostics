"""End-to-end workflow runs: triage -> specialists -> RCA Coordinator."""

import pytest

from hpc_agent.models import HypothesisStatus
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.diagnostic_workflow import render_report, run_diagnosis
from tests.conftest import make_data_root


@pytest.fixture
def diagnose(adapter, gateway, scopes):
    return lambda incident_id: run_diagnosis(adapter.load_incident(incident_id), gateway, scopes)


def causes(run, status=HypothesisStatus.HYPOTHESIS):
    return [c for c in run.rca.probable_causes if c.status is status]


def test_storage_001_refuses_unsupported_conclusion(diagnose):
    run = diagnose("POC-STORAGE-001")
    rca = run.rca
    assert rca.classification == "storage"
    assert [d.agent for d in run.diagnoses] == ["storage-agent", "scheduler-agent"]

    [hypothesis] = causes(run)
    assert hypothesis.cause == "Storage capacity or OST imbalance may be contributing to the write failure"
    assert hypothesis.confidence < 0.5
    assert rca.confirmed_root_cause is None and not run.root_cause_determined
    assert rca.recommended_actions == []

    assert [e.statement for e in rca.facts] == ["job failed while writing output", "filesystem utilization is high"]
    for item in ["actual filesystem utilization percentage", "individual OST utilization",
                 "filesystem health status",
                 "job termination details (state, exit code, termination reason)"]:
        assert item in rca.missing_information
    for check in ["get_filesystem_usage()", "get_ost_usage()", "get_job_status()"]:
        assert check in rca.recommended_checks

    assert any("storage and scheduler" in r for r in rca.cross_domain_relationships)
    text = " ".join([rca.summary] + [c.cause for c in rca.probable_causes] + [d.finding for d in run.diagnoses])
    assert "OST capacity caused" not in text and "caused the failure" not in text.lower()


def test_scheduler_001_keeps_uncertainty(diagnose):
    run = diagnose("POC-SCHEDULER-001")
    rca = run.rca
    assert rca.classification == "scheduler"
    open_causes = causes(run)
    assert {c.confidence for c in open_causes} == {0.3}
    assert any("SIGKILL" in c.cause for c in open_causes)
    assert any("sim-node-0042" in c.cause and "does not show" in c.cause for c in open_causes)
    assert "tied at confidence 0.30" in rca.summary

    [ruled_out] = causes(run, HypothesisStatus.RULED_OUT)
    assert ruled_out.cause == "Job reached its walltime limit"
    assert rca.conflicting_evidence == ruled_out.conflicting_evidence

    assert "reason node sim-node-0042 entered DRAIN" in rca.missing_information
    for domain in ("storage", "network", "gpu"):
        assert any(f"{domain} domain not examined" in c and "sim-node-0042" in c for c in rca.recommended_checks)
    assert rca.confirmed_root_cause is None


def test_gpu_001_does_not_conclude_hardware(diagnose):
    run = diagnose("POC-GPU-001")
    rca = run.rca
    assert rca.classification == "gpu"
    assert run.unavailable_agents == []
    assert [d.agent for d in run.diagnoses] == ["gpu-agent", "scheduler-agent"]

    [lead] = causes(run)
    assert "XID 13" in lead.cause and "commonly associated with application errors" in lead.cause
    assert not any("hardware fault" in c.cause for c in rca.probable_causes)
    assert rca.recommended_actions == []
    assert rca.confirmed_root_cause is None

    gpu = run.diagnoses[0]
    assert "No hardware failure can be concluded" in gpu.finding
    assert any("ECC double-bit error counter" in m and "NVLink" in m for m in rca.missing_information)
    for item in ["driver and CUDA versions", "whether the error reproduces on other GPUs or nodes"]:
        assert item in rca.missing_information
    assert any("DCGM" in c and "not executed by this platform" in c for c in rca.recommended_checks)


def test_multi_domain_correlation_on_shared_node(tmp_path, scopes):
    incident = {
        "incident_id": "SIM-MULTI-001", "severity": "high", "node": "sim-node-0100", "job_id": "SIM-JOB-9",
        "symptoms": ["job terminated", "network link down alert on node"],
    }
    adapter = make_data_root(tmp_path, incident, {
        "scheduler/job_status": {"job_id": "SIM-JOB-9", "state": "FAILED", "exit_code": 143,
                                 "requested_walltime": "01:00:00", "elapsed": "00:10:00",
                                 "allocated_nodes": ["sim-node-0100"], "termination_reason": None},
        "scheduler/node_status": {"nodes": [{"node": "sim-node-0100", "state": "DOWN", "reason": None}]},
        "network/network_status": {"interfaces": [{"node": "sim-node-0100", "interface": "bond0", "state": "down",
                                                   "rx_errors": 0, "tx_errors": 0, "link_flaps": 2}]},
    })
    run = run_diagnosis(adapter.load_incident("SIM-MULTI-001"), build_default_gateway(adapter), scopes)
    rca = run.rca
    assert rca.classification == "multi-domain"
    assert set(rca.affected_domains) == {"scheduler", "network"}
    assert "node:sim-node-0100 is implicated by network and scheduler findings" in rca.cross_domain_relationships
    assert causes(run)[0].domain == "network" and causes(run)[0].confidence == 0.5
    assert rca.confirmed_root_cause is None


def test_unknown_incident_still_produces_rca(tmp_path, scopes):
    adapter = make_data_root(tmp_path, {"incident_id": "SIM-UNK-001", "severity": "low", "symptoms": ["users unhappy"]})
    run = run_diagnosis(adapter.load_incident("SIM-UNK-001"), build_default_gateway(adapter), scopes)
    assert run.diagnoses == [] and run.tools_invoked == []
    assert run.rca.classification == "unknown"
    assert "No hypothesis is supported" in run.rca.summary


@pytest.mark.parametrize("incident_id", ["POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001"])
def test_report_separates_sections(diagnose, incident_id):
    report = render_report(diagnose(incident_id))
    for header in ["ROOT CAUSE: NOT CONFIRMED", "SUMMARY", "FACTS", "HYPOTHESES (ranked, unconfirmed)",
                   "RULED OUT", "TIMELINE", "CROSS-DOMAIN RELATIONSHIPS", "MISSING INFORMATION",
                   "RECOMMENDED CHECKS", "REMEDIATION PLAN (remediation-agent; proposals only",
                   "REFERENCES (knowledge base: context only", "TOOLS INVOKED"]:
        assert header in report


def test_runs_are_deterministic(diagnose):
    a, b = diagnose("POC-GPU-001"), diagnose("POC-GPU-001")
    assert a.run_id != b.run_id
    assert a.diagnoses == b.diagnoses and a.rca == b.rca


# --- Iteration 4: knowledge references and temporal correlation -----------------------
def reference_ids(run):
    return [r.source.rsplit("/", 1)[-1].removesuffix(".md") for r in run.rca.references]


def test_storage_001_references_do_not_change_conclusion(diagnose):
    run = diagnose("POC-STORAGE-001")
    assert reference_ids(run)[:3] == ["SOP-STOR-001", "SIM-HIST-0001", "MAN-STOR-OST-BALANCE"]
    assert "consult SOP-STOR-001 'Investigating job write failures on FEFS/Lustre' (reference only)" in run.rca.recommended_checks
    # A similar past incident (single full OST) is context, not evidence: still a 0.25 hypothesis.
    assert [c.confidence for c in run.rca.probable_causes] == [0.25]
    assert run.rca.timeline == []


def test_scheduler_001_timeline(diagnose):
    run = diagnose("POC-SCHEDULER-001")
    assert [e.statement for e in run.rca.timeline] == [
        "node sim-node-0043 state=ALLOCATED", "node sim-node-0042 state=DRAIN",
        "SIM-JOB-20001 state=FAILED", "SIM-JOB-20001 exit_code=137",
    ]
    rel = " | ".join(run.rca.cross_domain_relationships)
    assert "node sim-node-0042 state=DRAIN (2 min before)" in rel
    assert "SIM-JOB-20001 state=FAILED (20 s before)" in rel
    assert "halved" not in rel
    assert {"SOP-SCHED-001", "SIM-HIST-0002", "SIM-HIST-0003"} <= set(reference_ids(run))


def test_gpu_001_references_point_to_software_first(diagnose):
    run = diagnose("POC-GPU-001")
    assert reference_ids(run) == ["SOP-GPU-001", "SIM-HIST-0004", "MAN-GPU-XID"]
    assert "sim-gpu-node-0007 GPU 3 XID 13 count=1 (8 s before)" in " ".join(run.rca.cross_domain_relationships)
    assert not any("hardware fault" in c.cause for c in run.rca.probable_causes)


# --- remediation agent (處置) -----------------------------------------------------------
@pytest.mark.parametrize("incident_id,deferred", [
    ("POC-STORAGE-001", {"storage.capacity"}),
    ("POC-SCHEDULER-001", {"scheduler.signal_termination", "node.unhealthy"}),
    ("POC-GPU-001", {"gpu.application"}),
])
def test_poc_scenarios_get_no_remediation_and_say_why(diagnose, incident_id, deferred):
    run = diagnose(incident_id)
    plan = run.remediation
    assert run.rca.recommended_actions == []
    assert plan.summary.startswith("No remediation proposed") and plan.executes is False
    assert {d.category for d in plan.deferred} == deferred
    assert all(d.reason for d in plan.deferred)


def test_supported_storage_case_gets_playbook_remediation(tmp_path, scopes):
    incident = {"incident_id": "SIM-REM-001", "severity": "high", "symptoms": ["job failed while writing output"]}
    adapter = make_data_root(tmp_path, incident, {
        "storage/filesystem_usage": {"filesystem": "simfs01", "used_pct": 83, "health": "healthy"},
        "storage/ost_usage": {"osts": [{"ost": "OST0000", "used_pct": 80}, {"ost": "OST0001", "used_pct": 98}]},
    })
    run = run_diagnosis(adapter.load_incident("SIM-REM-001"), build_default_gateway(adapter), scopes)
    actions = {a.addresses: a for a in run.rca.recommended_actions}
    capacity = actions["storage.capacity"]
    assert capacity.affected_component == "OST0001" and capacity.reference == "SOP-STOR-001"
    assert capacity.verification and capacity.rollback
    assert run.rca.confirmed_root_cause is None  # proposing remediation never confirms the cause
    report = render_report(run)
    assert "verify first:" in report and "reference: SOP-STOR-001" in report
