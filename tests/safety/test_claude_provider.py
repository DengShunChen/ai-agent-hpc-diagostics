"""Claude provider (hybrid mode): message conversion, outbound checks and the data gate.

A fake client stands in for the Anthropic API: no request leaves this machine and nothing is billed.
"""

from types import SimpleNamespace as NS

import pytest

from hpc_agent.agents.llm_agents import LLMSpecialist
from hpc_agent.agents.scheduler import SchedulerAgent
from hpc_agent.llm.claude import (
    ClaudeProvider,
    OutboundBlockedError,
    cloud_data_problem,
    outbound_problems,
    to_claude_messages,
    to_claude_tools,
)
from hpc_agent.llm.provider import LLMConfigError, LLMUnavailableError
from hpc_agent.models import Domain
from hpc_agent.tools.gateway import ScopedGateway


def text(t):
    return NS(type="text", text=t)


def tool_use(id_, name, args=None):
    return NS(type="tool_use", id=id_, name=name, input=args or {})


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        content, stop = self.responses.pop(0)
        return NS(content=content, stop_reason=stop, stop_details=NS(category="cyber") if stop == "refusal" else None)


def provider(*responses):
    client = FakeClient(*responses)
    return ClaudeProvider(client=client), client


def test_request_uses_effort_and_server_side_fallback():
    llm, client = provider(([text('{"a": 1}')], "end_turn"))
    assert llm.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])["content"] == '{"a": 1}'
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5" and request["output_config"] == {"effort": "high"}
    assert request["betas"] == ["server-side-fallback-2026-07-01"] and request["fallbacks"] == "default"
    assert request["system"] == "sys" and "tools" not in request


def test_tools_become_strict_claude_tools():
    [tool] = to_claude_tools([{"type": "function", "function": {
        "name": "search_sop", "description": "d",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}])
    assert tool["name"] == "search_sop" and tool["strict"] is True
    assert tool["input_schema"]["additionalProperties"] is False


def test_conversation_passes_assistant_blocks_back_and_groups_tool_results():
    blocks = [NS(type="thinking", thinking=""), tool_use("tu_1", "a"), tool_use("tu_2", "b")]
    system, messages = to_claude_messages([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [], "provider_state": blocks},
        {"role": "tool", "tool_name": "a", "tool_call_id": "tu_1", "content": "ra"},
        {"role": "tool", "tool_name": "b", "tool_call_id": "tu_2", "content": "rb"},
        {"role": "user", "content": "Answer now with the JSON object only."},
    ])
    assert system == "sys" and messages[1] == {"role": "assistant", "content": blocks}
    last = messages[2]
    assert last["role"] == "user" and [b["type"] for b in last["content"]] == ["tool_result", "tool_result", "text"]
    assert [b.get("tool_use_id") for b in last["content"][:2]] == ["tu_1", "tu_2"]


def test_specialist_runs_on_claude_through_the_harness(adapter, gateway, scopes):
    drain = {"ref": "T2", "field": "state", "value": "DRAIN", "where": "sim-node-0042"}
    answer = ('{"finding": "Node sim-node-0042 was drained.", "hypotheses": [{"cause": "The node was drained",'
              ' "category": "node.unhealthy", "confidence": 0.5, "facts": [' + __import__("json").dumps(drain) + ']}]}')
    llm, client = provider(([tool_use("tu_1", "get_job_status"), tool_use("tu_2", "get_node_status")], "tool_use"),
                           ([text(answer)], "end_turn"))
    agent = LLMSpecialist(SchedulerAgent(), Domain.SCHEDULER, llm)
    d = agent.diagnose(adapter.load_incident("POC-SCHEDULER-001"), ScopedGateway(gateway, "scheduler-agent", scopes, []))
    assert d.reasoning == "llm" and d.reasoning_note.startswith("anthropic:claude-opus-5-5")
    assert [c.category for c in d.hypotheses] == ["node.unhealthy"]
    second = client.requests[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0].id == "tu_1"
    assert [b["tool_use_id"] for b in second[2]["content"]] == ["tu_1", "tu_2"]


def test_identifying_data_is_never_sent(adapter, gateway, scopes, tmp_path):
    from tests.conftest import make_data_root

    data = make_data_root(tmp_path, {"incident_id": "SIM-X-001", "severity": "high",
                                     "logs": ["connection reset by 10.20.30.40"]})
    llm, client = provider(([text("{}")], "end_turn"))
    agent = LLMSpecialist(SchedulerAgent(), Domain.SCHEDULER, llm)
    d = agent.diagnose(data.load_incident("SIM-X-001"), ScopedGateway(gateway, "scheduler-agent", scopes, []))
    assert client.requests == []  # blocked before anything left
    assert d.reasoning == "rules-fallback" and "request not sent to Claude: IPv4 address" in d.reasoning_note
    assert "10.20.30.40" not in d.reasoning_note


def test_outbound_scan_reports_kinds_not_values():
    problems = outbound_problems(["token: ghp_abcdefghijklmnop", "mail ops@example.com"])
    assert problems == ["credential-like content", "domain name", "e-mail address"]
    assert not any("ghp_" in p or "example.com" in p for p in problems)
    with pytest.raises(OutboundBlockedError):
        ClaudeProvider(client=FakeClient()).generate("s", "password=hunter2")


def test_refusal_and_truncation_fall_back():
    for stop in ("refusal", "max_tokens"):
        llm, _ = provider(([text("x")], stop))
        with pytest.raises(LLMUnavailableError):
            llm.chat([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("model, effort", [("gpt-5", "high"), ("claude-opus-5-5", "turbo")])
def test_invalid_configuration_is_refused(model, effort):
    with pytest.raises(LLMConfigError):
        ClaudeProvider(model=model, effort=effort, client=FakeClient())


def test_cloud_data_gate():
    assert cloud_data_problem("synthetic") is None and cloud_data_problem("deidentified-import") is None
    assert "import-export" in cloud_data_problem(None)


def test_cli_refuses_claude_on_unmarked_data(tmp_path, monkeypatch, capsys):
    from hpc_agent.main import main

    (tmp_path / "fixtures" / "incidents").mkdir(parents=True)
    monkeypatch.setenv("HPC_AGENT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("HPC_AGENT_REASONING", "llm")
    assert main(["diagnose", "SIM-X-001", "--no-save"]) == 2
    assert "DATA_CLASS.json" in capsys.readouterr().err


def test_evaluation_refuses_claude_on_non_synthetic_cases(tmp_path):
    import json

    from hpc_agent.evaluation.harness import evaluate
    from tests.conftest import PROJECT_ROOT

    case = json.loads((PROJECT_ROOT / "evaluation" / "cases" / "EVAL-GPU-001.json").read_text())
    case["label_source"] = "site-a; labelled by site engineers"
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "EVAL-GPU-001.json").write_text(json.dumps(case))
    with pytest.raises(PermissionError):
        evaluate(tmp_path / "cases", llm=ClaudeProvider(client=FakeClient()))
