"""Phase B: permission levels, PreToolUse / PostToolUse hooks, and per-run agent traces."""

import pytest

from hpc_agent.agents.llm_agents import LLMSpecialist
from hpc_agent.agents.scheduler import SchedulerAgent
from hpc_agent.models import Domain
from hpc_agent.runtime.definitions import DefinitionError, load_definitions, parse_definition
from hpc_agent.runtime.tools import ToolSession, run_tool
from hpc_agent.runtime.trace import TraceRecorder
from hpc_agent.safety.policy import Operation
from hpc_agent.tools.gateway import ScopedGateway, ToolGateway, ToolResult, ToolSpec, ToolStatus
from tests.conftest import PROJECT_ROOT
from tests.safety.test_llm_agents import DRAIN, FakeLLM, answer, calls


def scheduler_with(definition_changes, llm, trace=None):
    definition = load_definitions(PROJECT_ROOT)["scheduler-agent"].model_copy(update=definition_changes)
    return LLMSpecialist(SchedulerAgent(), Domain.SCHEDULER, llm, definition=definition,
                         emit=trace.for_agent("scheduler-agent") if trace else (lambda *a, **k: None))


def test_ask_permission_is_never_run_unattended(adapter, gateway, scopes):
    trace, log = TraceRecorder(), []
    llm = FakeLLM({"scheduler": [calls("get_job_status", "get_node_status"), answer(hypotheses=[])]})
    agent = scheduler_with({"ask": ("get_node_status",)}, llm, trace)
    agent.diagnose(adapter.load_incident("POC-SCHEDULER-001"), ScopedGateway(gateway, "scheduler-agent", scopes, log))
    assert [c.tool for c in log] == ["get_job_status"]
    [denied] = [e for e in trace.events if e["kind"] == "tool_call" and e["decision"] == "denied"]
    assert denied["tool"] == "get_node_status" and denied["permission"] == "ask" and "human" in denied["reason"]


def test_out_of_scope_calls_are_denied_and_traced(adapter, gateway, scopes):
    trace = TraceRecorder()
    llm = FakeLLM({"scheduler": [calls("get_gpu_health"), answer(hypotheses=[])]})
    scheduler_with({}, llm, trace).diagnose(adapter.load_incident("POC-SCHEDULER-001"),
                                            ScopedGateway(gateway, "scheduler-agent", scopes, []))
    [denied] = [e for e in trace.events if e["kind"] == "tool_call"]
    assert denied["permission"] == "deny" and denied["decision"] == "denied"


def test_post_tool_use_withholds_credential_like_results(adapter):
    registry = ToolGateway()
    registry.register(ToolSpec(
        name="get_leaky_status", description="test", operation=Operation.READ_TEST_DATA,
        parameters=frozenset({"incident_id"}),
        handler=lambda incident_id: ToolResult(tool="get_leaky_status", status=ToolStatus.OK, source="x",
                                               data={"note": "password=hunter2"})))
    trace = TraceRecorder()
    session = ToolSession(max_calls=3, emit=trace.for_agent("test-agent"))
    scoped = ScopedGateway(registry, "test-agent", {"test-agent": frozenset({"get_leaky_status"})}, [])
    text = run_tool("get_leaky_status", {}, adapter.load_incident("POC-SCHEDULER-001"), scoped, session, "x")
    assert text.startswith("error: result withheld (SENSITIVE INFORMATION DETECTED AND NOT PROCESSED.")
    assert session.fetched == {}
    assert "hunter2" not in str(trace.events) and trace.events[-1]["status"] == "withheld"


def test_ask_must_be_a_subset_of_tools():
    with pytest.raises(DefinitionError):
        parse_definition("---\nname: x-agent\ndescription: x\ndomain: x\ntools: [a]\nask: [b]\n---\n", "x")


def test_service_stores_a_verifiable_trace_per_run(tmp_path, adapter, gateway, scopes):
    from hpc_agent.workflows.approval_workflow import ApprovalService

    llm = FakeLLM({"scheduler": [calls("get_job_status", "get_node_status"), answer(
        finding="Node sim-node-0042 was drained.",
        hypotheses=[{"cause": "Node drained", "category": "node.unhealthy", "confidence": 0.5, "facts": [DRAIN]}])],
        "triage": [answer(domains=[{"domain": "scheduler", "items": ["I1"]}], primary="scheduler")]})
    service = ApprovalService(tmp_path / "state", adapter, gateway, scopes, reasoning_llm=llm)
    run = service.diagnose(adapter.load_incident("POC-SCHEDULER-001"))
    events = service.trace(run.run_id)
    kinds = {(e["agent"], e["kind"]) for e in events}
    assert {("triage-agent", "prompt"), ("triage-agent", "checks"), ("scheduler-agent", "prompt"),
            ("scheduler-agent", "model_reply"), ("scheduler-agent", "tool_call"), ("scheduler-agent", "tool_result"),
            ("scheduler-agent", "checks")} <= kinds
    [checks] = [e for e in events if e["agent"] == "scheduler-agent" and e["kind"] == "checks"]
    assert checks["kept"] == ["hypothesis: node.unhealthy (0.50)"]
    allowed = [e for e in events if e["kind"] == "tool_call" and e["decision"] == "allowed"]
    assert all(e["used"] == {"incident_id": "POC-SCHEDULER-001"} for e in allowed)
    assert service.verify_traces() == (1, len(events))


def test_rule_based_runs_have_no_trace(service, adapter):
    run = service.diagnose(adapter.load_incident("POC-SCHEDULER-001"))
    assert service.trace(run.run_id) == [] and service.verify_traces() == (0, 0)


def test_trace_withholds_credential_like_model_text():
    trace = TraceRecorder()
    trace.record("x-agent", "model_reply", content="here is password=hunter2")
    assert "hunter2" not in str(trace.events) and "withheld" in trace.events[0]


def test_trace_api_requires_auth_and_validates_the_run(api, adapter):
    run_id = api.post("/api/runs", json={"incident_id": "POC-SCHEDULER-001"}, headers=api.auth_header()).json()["run_id"]
    assert api.get(f"/api/runs/{run_id}/trace").status_code == 401
    assert api.get(f"/api/runs/{run_id}/trace", headers=api.auth_header()).json() == []
    assert api.get("/api/runs/run-000000000000/trace", headers=api.auth_header()).status_code == 404
    assert api.get("/api/runs/..%2Fsecrets/trace", headers=api.auth_header()).status_code == 404


def test_cli_trace_and_verify_audit(tmp_path, monkeypatch, capsys):
    from hpc_agent.main import main

    monkeypatch.setenv("HPC_AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert main(["diagnose", "POC-SCHEDULER-001"]) == 0
    run_id = next(w for w in capsys.readouterr().out.split() if w.startswith("run-"))
    assert main(["trace", run_id]) == 0
    assert "no agent trace (rule-based run)" in capsys.readouterr().out
    assert main(["verify-audit"]) == 0
    assert "traces/: 0 run trace(s)" in capsys.readouterr().out
