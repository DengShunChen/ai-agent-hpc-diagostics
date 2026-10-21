import pytest

from hpc_agent.models import ApprovalStatus as S
from hpc_agent.safety.approval import InvalidTransitionError
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.approval_workflow import (
    ApprovalService,
    UnknownRunError,
    build_approval_request,
    render_review,
)
from tests.conftest import make_data_root


def test_diagnose_records_run_awaiting_approval(service, adapter):
    run = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    assert [r.status for r in service.history(run.run_id)] == [
        S.DRAFT, S.DIAGNOSIS_COMPLETE, S.AWAITING_HUMAN_APPROVAL]
    assert all(r.operator is None for r in service.history(run.run_id))
    assert service.runs.load(run.run_id) == run
    [audit] = service.audit.records()
    assert audit["workflow_stage"] == "AWAITING_HUMAN_APPROVAL"
    assert audit["agents_invoked"] == ["triage-agent", "storage-agent", "scheduler-agent", "rca-coordinator"]
    assert {t["tool"] for t in audit["tools_invoked"]} >= {"get_filesystem_usage", "get_ost_usage"}
    assert "POC-STORAGE-001:symptoms[0]" in audit["evidence_used"]
    assert "get_ost_usage()" in audit["recommendations"]


def test_approve_then_close(service, adapter):
    run = service.diagnose(adapter.load_incident("POC-SCHEDULER-001"))
    approved = service.decide(run.run_id, S.APPROVED, "ops.wang", "proceed with checks")
    assert approved.status is S.APPROVED and approved.operator == "ops.wang"
    assert service.close(run.run_id, "ops.wang").status is S.CLOSED
    audit = service.audit.records()
    assert [(a["approval_status"], a["operator"]) for a in audit[1:]] == [("APPROVED", "ops.wang"), ("CLOSED", "ops.wang")]


@pytest.mark.parametrize("decision", [S.REJECTED, S.REVISION_REQUESTED])
def test_negative_decisions_need_comment(service, adapter, decision):
    run = service.diagnose(adapter.load_incident("POC-GPU-001"))
    with pytest.raises(ValueError, match="comment"):
        service.decide(run.run_id, decision, "ops.wang", "  ")
    assert service.status(run.run_id).status is S.AWAITING_HUMAN_APPROVAL


def test_revise_creates_superseding_run(service, adapter):
    old = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    service.decide(old.run_id, S.REJECTED, "ops.lee", "need OST data")
    new = service.revise(old.run_id, "ops.lee")
    assert new.run_id != old.run_id and new.supersedes == old.run_id
    assert service.status(old.run_id).status is S.REVISE
    assert service.status(new.run_id).status is S.AWAITING_HUMAN_APPROVAL
    assert service.audit.records()[-1]["operator_comment"] == f"supersedes {old.run_id}"


@pytest.mark.parametrize("decision", [S.CLOSED, S.REVISE, S.DRAFT, S.AWAITING_HUMAN_APPROVAL])
def test_decide_accepts_only_human_decisions(service, adapter, decision):
    run = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    with pytest.raises(InvalidTransitionError):
        service.decide(run.run_id, decision, "ops.wang", "x")


def test_close_and_revise_need_prior_decision(service, adapter):
    run = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    with pytest.raises(InvalidTransitionError):
        service.close(run.run_id, "ops.wang")
    with pytest.raises(InvalidTransitionError):
        service.revise(run.run_id, "ops.wang")


@pytest.mark.parametrize("run_id", ["run-000000000000", "run-../../etc", "", "RUN-ABC"])
def test_unknown_run(service, run_id):
    with pytest.raises(UnknownRunError):
        service.review(run_id)


def test_run_snapshots_are_write_once(service, adapter):
    run = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    with pytest.raises(FileExistsError):
        service.runs.save(run)


def test_forged_transition_in_log_is_detected(service, adapter):
    run = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    forged = service.status(run.run_id).model_dump(mode="json") | {"status": "CLOSED"}
    service.approvals.append(forged)  # valid hash chain, invalid state transition
    with pytest.raises(InvalidTransitionError):
        service.status(run.run_id)


def test_state_dir_inside_data_root_rejected(adapter, gateway):
    with pytest.raises(ValueError, match="read-only data root"):
        ApprovalService(adapter.root / "knowledge" / "state", adapter, gateway)


def test_review_package_for_action(tmp_path, scopes):
    incident = {"incident_id": "SIM-GPU-HW-001", "severity": "high", "node": "sim-gpu-node-0001",
                "symptoms": ["GPU fell off the bus during training job"]}
    adapter = make_data_root(tmp_path / "data", incident, {"gpu/gpu_health": {"node": "sim-gpu-node-0001", "gpus": [
        {"index": 2, "health": "fail", "xid_events": [{"xid": 79, "count": 1}], "temperature_c": 70,
         "power_w": 200, "ecc_dbe_volatile": 0, "pcie_replay_count": 0, "nvlink_errors": 0}]}})
    svc = ApprovalService(tmp_path / "state", adapter, build_default_gateway(adapter), scopes)
    run = svc.diagnose(adapter.load_incident("SIM-GPU-HW-001"))
    request = svc.review(run.run_id)
    [action] = request.actions
    assert action.proposed_action == "Drain sim-gpu-node-0001 from scheduling pending operator-run GPU diagnostics"
    assert action.reason and action.supporting_evidence and action.risk == "medium"
    assert action.affected_component == "sim-gpu-node-0001 GPU 2"
    assert action.confidence == 0.5
    assert action.missing_information == run.rca.missing_information
    text = render_review(request)
    for label in ["Proposed Action:", "Reason:", "Supporting Evidence:", "Risk:", "Affected Component:",
                  "Confidence:", "Missing Information:"]:
        assert label in text
    assert "this platform executes nothing" in text


def test_review_without_actions_explains_scope(service, adapter):
    run = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    request = build_approval_request(run, S.AWAITING_HUMAN_APPROVAL)
    assert request.actions == []
    assert "No remediation action is proposed" in request.note
    assert request.hypotheses == [
        "(storage, 0.25) Storage capacity or OST imbalance may be contributing to the write failure"]
