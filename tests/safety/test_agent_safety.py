"""Agents reach data only through scoped tools, never invent evidence, and fail closed."""

import json
import re

import pytest

from hpc_agent.models import EvidenceKind
from hpc_agent.tools.gateway import ScopedGateway, ToolNotAllowedError
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis
from tests.conftest import PROJECT_ROOT

SCENARIOS = ["POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001"]


def test_agent_cannot_call_tool_outside_scope(gateway, scopes):
    scoped = ScopedGateway(gateway, "storage-agent", scopes, [])
    with pytest.raises(ToolNotAllowedError):
        scoped.invoke("get_gpu_health", incident_id="POC-GPU-001")


def test_unknown_agent_has_no_tools(gateway, scopes):
    assert ScopedGateway(gateway, "rogue-agent", scopes, []).allowed_tools == frozenset()


def test_config_cannot_widen_gateway_allowlist(gateway):
    scopes = {"storage-agent": frozenset({"run_shell", "get_ost_usage"})}
    scoped = ScopedGateway(gateway, "storage-agent", scopes, [])
    assert scoped.allowed_tools == {"get_ost_usage"}
    with pytest.raises(ToolNotAllowedError):
        scoped.invoke("run_shell", incident_id="POC-STORAGE-001")


def test_denied_call_is_recorded(gateway):
    log = []
    scoped = ScopedGateway(gateway, "storage-agent", {"storage-agent": frozenset({"get_ost_usage"})}, log)
    with pytest.raises(Exception):
        scoped.invoke("get_ost_usage", incident_id="../etc")
    assert [(r.tool, r.result_status) for r in log] == [("get_ost_usage", "error")]


def test_workflow_fails_closed_when_agent_lacks_scope(adapter, gateway):
    with pytest.raises(ToolNotAllowedError):
        run_diagnosis(adapter.load_incident("POC-STORAGE-001"), gateway, scopes={})


@pytest.fixture(params=SCENARIOS)
def run(request, adapter, gateway, scopes):
    return run_diagnosis(adapter.load_incident(request.param), gateway, scopes)


def test_no_run_claims_a_root_cause(run):
    assert run.root_cause_determined is False
    assert run.human_approval_required is True
    assert all(d.human_approval_required for d in run.diagnoses)


def test_reported_evidence_is_verbatim(run, adapter):
    incident = adapter.load_incident(run.incident_id)
    for e in run.facts:
        if e.kind is EvidenceKind.INCIDENT_REPORT:
            incident_id, ref = e.source.split(":")
            field, index = ref.rstrip("]").split("[")
            assert incident_id == run.incident_id
            assert getattr(incident, field)[int(index)] == e.statement


def test_tool_evidence_traces_to_saved_data(run):
    for e in run.facts:
        if e.kind is EvidenceKind.TOOL_RESULT:
            _, tool, path = e.source.split(":", 2)
            assert (PROJECT_ROOT / path).is_file()
            saved = json.loads((PROJECT_ROOT / path).read_text())
            # every number in the statement must appear in the saved file
            for value in re.findall(r"\d+(?:\.\d+)?", e.statement.replace(run.incident_id, "")):
                assert value in json.dumps(saved), f"{value!r} not in {path}"
            assert any(r.tool == tool and r.evidence_reference == path for r in run.tools_invoked)


def test_tool_calls_stay_within_scopes(run, scopes):
    for record in run.tools_invoked:
        assert record.tool in scopes[record.agent]


def test_recommended_actions_have_tool_evidence(run):
    for d in run.diagnoses:
        for action in d.recommended_actions:
            assert any(e.kind is EvidenceKind.TOOL_RESULT for e in action.supporting_evidence)

