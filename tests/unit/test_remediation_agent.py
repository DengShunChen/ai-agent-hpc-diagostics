import pytest

from hpc_agent.agents.remediation import PLAYBOOK, RemediationAgent
from hpc_agent.models import Evidence, EvidenceKind, ProbableCause, RCAResult
from hpc_agent.models.site import SiteProfile

TOOL = Evidence(kind=EvidenceKind.TOOL_RESULT, source="tool:get_ost_usage:x", statement="OST0002=97%")
REPORT = Evidence(kind=EvidenceKind.INCIDENT_REPORT, source="INC-001:symptoms[0]", statement="write failed")


def cause(category, confidence=0.6, support=(TOOL,), components=("ost:OST0002",), **kw):
    return ProbableCause(cause=f"cause {category}", category=category, confidence=confidence,
                         supporting_evidence=list(support), components=list(components), **kw)


def plan(*causes, profile=None):
    rca = RCAResult(incident_id="INC-001", classification="storage", probable_causes=list(causes),
                    facts=[TOOL, REPORT], confidence=max((c.confidence for c in causes), default=0))
    return RemediationAgent(profile or SiteProfile(site_id="default")).plan(rca)


def test_tool_backed_confident_hypothesis_gets_playbook_action():
    [action], p = plan(cause("storage.capacity"))
    assert action.action == "Review and free capacity on OST0002 following the site storage SOP"
    assert action.risk == "medium" and action.affected_component == "OST0002"
    assert action.addresses == "storage.capacity" and action.reference == "SOP-STOR-001"
    assert action.verification and action.rollback
    assert action.supporting_evidence == [TOOL]
    assert p.deferred == [] and p.executes is False and p.human_approval_required is True


@pytest.mark.parametrize(
    "c,reason",
    [
        (cause("storage.capacity", 0.25, support=(REPORT,)), "no tool evidence"),
        (cause("storage.capacity", 0.35), "below the site threshold 0.40"),
        (cause("scheduler.signal_termination", 0.6, components=("job:SIM-JOB-1",)), "no playbook entry"),
        (cause("storage.capacity", 0.6, components=()), "no affected component"),
    ],
)
def test_deferred_with_reason(c, reason):
    actions, p = plan(c)
    assert actions == []
    assert reason in p.deferred[0].reason
    assert p.summary.startswith("No remediation proposed")


def test_ruled_out_causes_are_ignored():
    ruled = ProbableCause(cause="walltime", category="scheduler.walltime", status="ruled_out",
                          conflicting_evidence=[TOOL])
    actions, p = plan(ruled)
    assert actions == [] and p.deferred == []


def test_site_threshold_and_floor():
    strict = SiteProfile(site_id="strict", remediation_min_confidence=0.7)
    assert plan(cause("storage.capacity", 0.6), profile=strict)[0] == []
    with pytest.raises(ValueError):
        SiteProfile(site_id="lax", remediation_min_confidence=0.1)  # weak hypotheses never get actions


def test_gpu_and_network_targets():
    [gpu], _ = plan(cause("gpu.hardware", 0.5, components=("gpu:sim-gpu-node-0001/2", "node:sim-gpu-node-0001")))
    assert gpu.action == "Drain sim-gpu-node-0001 from scheduling pending operator-run GPU diagnostics"
    assert gpu.affected_component == "sim-gpu-node-0001 GPU 2"
    [net], _ = plan(cause("network.link_down", 0.5,
                          components=("interface:sim-node-0201/bond0", "node:sim-node-0201")))
    assert net.action == "Take sim-node-0201 out of scheduling and escalate sim-node-0201/bond0 to the network team"


def test_duplicate_actions_merged():
    actions, _ = plan(cause("storage.capacity"), cause("storage.capacity", 0.5))
    assert len(actions) == 1


def test_playbook_is_proposal_text_only():
    for category, play in PLAYBOOK.items():
        text = " ".join([play.action, play.reason, *play.verification, play.rollback or ""])
        for word in ("sudo", "rm -", "systemctl", "reboot", "ssh ", "`", "$(", "&&"):
            assert word not in text, (category, word)
        assert play.reference.startswith("SOP-")


def test_agent_has_no_tools_or_execution():
    from tests.conftest import PROJECT_ROOT

    source = (PROJECT_ROOT / "src" / "hpc_agent" / "agents" / "remediation.py").read_text()
    for forbidden in ("gateway", "invoke(", "data_adapter", "approval_workflow", "subprocess"):
        assert forbidden not in source
