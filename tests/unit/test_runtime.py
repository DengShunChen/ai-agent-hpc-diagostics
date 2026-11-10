"""The agent harness: definitions, the shared tool loop and tool execution."""

import pytest

from hpc_agent.runtime.definitions import DefinitionError, load_definitions, parse_definition
from hpc_agent.runtime.loop import ask_json, parse_json, run_agent
from hpc_agent.runtime.tools import ToolSession, run_tool, tool_specs
from hpc_agent.tools.gateway import ScopedGateway
from tests.conftest import PROJECT_ROOT


class Scripted:
    name, model = "fake", "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None, json_output=False):
        self.calls.append((list(messages), tools, json_output))
        return self.replies.pop(0)


def test_every_agent_has_a_definition_and_llm_agents_have_prompts():
    definitions = load_definitions(PROJECT_ROOT)
    assert set(definitions) == {"triage-agent", "storage-agent", "scheduler-agent", "network-agent", "gpu-agent",
                                "rca-coordinator", "remediation-agent"}
    for name in ("triage-agent", "storage-agent", "scheduler-agent", "network-agent", "gpu-agent"):
        assert definitions[name].llm is not None and definitions[name].prompt
    for name in ("rca-coordinator", "remediation-agent"):  # stay rule-based: no tools, no model
        assert definitions[name].llm is None and definitions[name].tools == ()


def test_render_requires_an_llm_prompt():
    with pytest.raises(DefinitionError):
        load_definitions(PROJECT_ROOT)["rca-coordinator"].render()
    with pytest.raises(DefinitionError):
        parse_definition("---\nname: x-agent\ndescription: x\ndomain: x\nllm:\n  categories: [Bad]\n---\nhi", "x")


def test_loop_runs_tools_then_returns_the_answer():
    llm = Scripted({"content": "", "tool_calls": [{"name": "t", "arguments": {}}]},
                   {"content": 'here: {"ok": 1}', "tool_calls": []})
    seen = []
    answer = run_agent(llm, "sys", "user", [{"type": "function"}], 4, lambda n, a: seen.append(n) or "result")
    assert answer == {"ok": 1} and seen == ["t"]
    assert llm.calls[1][0][-1] == {"role": "tool", "tool_name": "t", "content": "result"}


def test_loop_stops_at_max_rounds_and_asks_once_for_json():
    call = {"content": "", "tool_calls": [{"name": "t", "arguments": {}}]}
    llm = Scripted(call, call, {"content": "still no json", "tool_calls": []})
    assert run_agent(llm, "s", "u", [], 2, lambda n, a: "r") is None
    assert len(llm.calls) == 3 and llm.calls[-1][2] is True


def test_parse_json_and_ask_json():
    assert parse_json("x {\"a\": [1]} y") == {"a": [1]} and parse_json("[1]") is None and parse_json("") is None
    assert ask_json(Scripted({"content": '{"d": 1}', "tool_calls": []}), "s", "u") == {"d": 1}


def test_tool_execution_ignores_model_arguments_and_enforces_limits(adapter, gateway, scopes):
    log = []
    scoped = ScopedGateway(gateway, "scheduler-agent", scopes, log)
    incident = adapter.load_incident("POC-SCHEDULER-001")
    session = ToolSession(max_calls=1)
    assert {s["function"]["name"] for s in tool_specs(scoped)} == set(scopes["scheduler-agent"])
    text = run_tool("get_job_status", {"incident_id": "POC-GPU-001"}, incident, scoped, session, "scheduler")
    assert text.startswith("T1 = get_job_status") and log[0].evidence_reference.endswith("POC-SCHEDULER-001/job_status.json")
    assert run_tool("get_node_status", {}, incident, scoped, session, "x") == "error: tool call limit reached; answer now"
    assert run_tool("get_gpu_health", {}, incident, scoped, session, "x") == "error: get_gpu_health is not one of your tools"
    assert len(log) == 1
