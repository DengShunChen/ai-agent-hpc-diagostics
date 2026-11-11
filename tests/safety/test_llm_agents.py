"""LLM-driven agents: the model proposes; deterministic checks decide what a human sees.

A scripted fake model stands in for Ollama, so these tests cover the checks, not model quality.
"""

import json

import pytest

from hpc_agent.agents.llm_agents import LLMSpecialist, LLMTriageAgent, leaves, text_problem
from hpc_agent.agents.scheduler import SchedulerAgent
from hpc_agent.llm.provider import LLMUnavailableError
from hpc_agent.models import Domain, HypothesisStatus
from hpc_agent.safety.guards import verify_run
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis


class FakeLLM:
    """Answers each agent from a script keyed by the agent's domain (taken from the system prompt)."""

    name, model = "fake", "fake-7b"

    def __init__(self, scripts):
        self.scripts = scripts  # domain -> list of replies (dicts) or exceptions
        self.seen = []

    def chat(self, messages, tools=None, json_output=False):
        system = messages[0]["content"]
        domain = "triage" if "triage agent" in system else system.split(" the ", 1)[1].split()[0]
        self.seen.append((domain, [t["function"]["name"] for t in tools or []], messages))
        script = self.scripts.get(domain) or [{"content": "{}"}]
        reply = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(reply, Exception):
            raise reply
        return {"content": reply.get("content", ""), "tool_calls": reply.get("tool_calls", [])}


def calls(*names, **arguments):
    return {"tool_calls": [{"name": n, "arguments": dict(arguments)} for n in names]}


def answer(**body):
    return {"content": json.dumps(body)}


DRAIN = {"ref": "T2", "field": "state", "value": "DRAIN", "where": "sim-node-0042"}
EXIT = {"ref": "T1", "field": "exit_code", "value": 137}


def scheduler_run(adapter, gateway, scopes, final, extra_calls=None):
    script = [calls("get_job_status", "get_node_status", **(extra_calls or {})), answer(**final)]
    llm = FakeLLM({"scheduler": script, "triage": [answer(domains=[{"domain": "scheduler", "items": ["I1"]}],
                                                         primary="scheduler")]})
    return run_diagnosis(adapter.load_incident("POC-SCHEDULER-001"), gateway, scopes, llm=llm), llm


def scheduler(run):
    return next(d for d in run.diagnoses if d.agent == "scheduler-agent")


def test_grounded_hypothesis_is_kept_with_statements_written_from_data(adapter, gateway, scopes):
    run, _ = scheduler_run(adapter, gateway, scopes, {
        "finding": "Job exited with 137 while node sim-node-0042 was drained.",
        "hypotheses": [{"cause": "The node was drained and the job was killed", "category": "node.unhealthy",
                        "confidence": 0.5, "facts": [DRAIN, EXIT], "reported": ["I1"]}]})
    d = scheduler(run)
    assert d.reasoning == "llm" and d.reasoning_note.startswith("fake:fake-7b")
    [cause] = d.hypotheses
    assert cause.category == "node.unhealthy" and cause.confidence == 0.5
    statements = [e.statement for e in cause.supporting_evidence]
    assert "node=sim-node-0042: state=\"DRAIN\"" in statements
    assert any(s.endswith("exit_code=137") for s in statements)
    assert "node:sim-node-0042" in cause.components
    assert run.triage.reasoning == "llm"
    verify_run(run, scopes)  # every tool fact matches a real, successful, in-scope call


@pytest.mark.parametrize("fact, reason", [
    ({"ref": "T1", "field": "exit_code", "value": 9}, "has no exit_code=9"),
    ({"ref": "T7", "field": "exit_code", "value": 137}, "not a tool result this agent fetched"),
    ({"ref": "T2", "field": "state", "value": "DRAIN", "where": "sim-node-9999"}, "for sim-node-9999"),
    ("T1", "fact is not an object"),
])
def test_fabricated_or_unfetched_facts_discard_the_hypothesis(adapter, gateway, scopes, fact, reason):
    run, _ = scheduler_run(adapter, gateway, scopes, {
        "hypotheses": [{"cause": "Job killed", "category": "scheduler.signal_termination", "facts": [fact]}]})
    d = scheduler(run)
    assert d.hypotheses == [] and reason in d.discarded_proposals[0]


@pytest.mark.parametrize("cause, reason", [
    ("Run scontrol update to resume the node", "contains a command"),
    ("The root cause is the drained node", "claims a confirmed cause"),
    ("The job used 512 GB of memory", "numbers not found in the data: 512"),
])
def test_unsafe_or_unsupported_text_is_discarded(adapter, gateway, scopes, cause, reason):
    run, _ = scheduler_run(adapter, gateway, scopes, {
        "hypotheses": [{"cause": cause, "category": "node.unhealthy", "facts": [DRAIN]}]})
    d = scheduler(run)
    assert d.hypotheses == [] and reason in d.discarded_proposals[0]


def test_categories_are_fixed_and_confidence_is_capped(adapter, gateway, scopes):
    run, _ = scheduler_run(adapter, gateway, scopes, {"hypotheses": [
        {"cause": "Node drained", "category": "node.unhealthy", "confidence": 0.99, "facts": [DRAIN]},
        {"cause": "Cosmic rays", "category": "physics.cosmic", "facts": [DRAIN]},
        {"cause": "Job was preempted", "category": "scheduler.walltime", "confidence": 0.9, "reported": ["I1"]},
    ]})
    d = scheduler(run)
    by_category = {c.category: c for c in d.hypotheses}
    assert by_category["node.unhealthy"].confidence == 0.6  # cap with tool facts
    assert by_category["scheduler.walltime"].confidence == 0.3  # cap with reports only
    assert any("physics.cosmic" in p for p in d.discarded_proposals)


def test_nothing_can_be_confirmed_and_ruling_out_needs_data(adapter, gateway, scopes):
    run, _ = scheduler_run(adapter, gateway, scopes, {
        "hypotheses": [{"cause": "Node drained", "category": "node.unhealthy", "facts": [DRAIN], "status": "confirmed"}],
        "ruled_out": [{"cause": "Walltime", "category": "scheduler.walltime", "reported": ["I1"]},
                      {"cause": "Signal from the user", "category": "scheduler.signal_termination", "facts": [EXIT]}]})
    d = scheduler(run)
    assert all(c.status is not HypothesisStatus.CONFIRMED for c in d.possible_causes)
    ruled = [c for c in d.possible_causes if c.status is HypothesisStatus.RULED_OUT]
    assert [c.category for c in ruled] == ["scheduler.signal_termination"] and ruled[0].conflicting_evidence
    assert any("ruling a cause out needs cited tool data" in p for p in d.discarded_proposals)
    assert run.rca.confirmed_root_cause is None


def test_model_cannot_reach_tools_outside_scope_or_other_incidents(adapter, gateway, scopes):
    llm = FakeLLM({"scheduler": [calls("get_gpu_health", "run_shell", incident_id="POC-GPU-001"),
                                 calls("get_job_status", incident_id="POC-GPU-001"),
                                 answer(hypotheses=[])]})
    run = run_diagnosis(adapter.load_incident("POC-SCHEDULER-001"), gateway, scopes, llm=llm)
    tools = [(c.agent, c.tool, c.evidence_reference) for c in run.tools_invoked if c.agent == "scheduler-agent"]
    assert ("scheduler-agent", "get_job_status", "test_data/scheduler/POC-SCHEDULER-001/job_status.json") in tools
    assert not any(t in ("get_gpu_health", "run_shell") for _, t, _ in tools)
    offered = next(names for domain, names, _ in llm.seen if domain == "scheduler")
    assert set(offered) == set(scopes["scheduler-agent"])
    replies = [m["content"] for m in llm.seen[-1][2] if m["role"] == "tool"]
    assert "error: get_gpu_health is not one of your tools" in replies
    verify_run(run, scopes)


def test_missing_and_unconsulted_data_are_reported(adapter, gateway, scopes):
    llm = FakeLLM({"storage": [calls("get_filesystem_usage"), answer(hypotheses=[])]})
    run = run_diagnosis(adapter.load_incident("POC-STORAGE-001"), gateway, scopes, llm=llm)
    storage = next(d for d in run.diagnoses if d.agent == "storage-agent")
    assert "no saved data from get_filesystem_usage" in storage.missing_information
    assert "get_ost_usage was not consulted by the model" in storage.missing_information
    assert storage.finding == "" and storage.hypotheses == []


@pytest.mark.parametrize("failure", [LLMUnavailableError("connection refused"), {"content": "not json"}])
def test_unusable_model_falls_back_to_rules(adapter, gateway, scopes, failure):
    incident = adapter.load_incident("POC-SCHEDULER-001")
    llm = FakeLLM({"scheduler": [failure], "triage": [failure]})
    run = run_diagnosis(incident, gateway, scopes, llm=llm)
    rules = run_diagnosis(incident, gateway, scopes)
    d = scheduler(run)
    assert d.reasoning == "rules-fallback" and "fake-7b" in d.reasoning_note
    assert run.triage.reasoning == "rules-fallback"
    assert [c.category for c in d.possible_causes] == [c.category for c in scheduler(rules).possible_causes]


def test_triage_rules_can_add_domains_but_never_remove(adapter):
    incident = adapter.load_incident("POC-SCHEDULER-001")
    llm = FakeLLM({"triage": [answer(domains=[{"domain": "network", "items": ["I1"]},
                                              {"domain": "kernel", "items": ["I1"]},
                                              {"domain": "gpu", "items": ["I99"]}], primary="network")]})
    result = LLMTriageAgent(llm).triage(incident)
    assert result.reasoning == "llm"
    assert Domain.NETWORK in result.domain_signals and Domain.SCHEDULER in result.domain_signals
    assert Domain.GPU not in result.domain_signals  # cited a non-existent item
    assert "keyword rules added: scheduler" in result.reasoning_note
    assert result.selected_agents[0] == "network-agent"


def test_leaves_carry_record_identifiers_and_time():
    data = {"node": "n1", "gpus": [{"index": 3, "xid_events": [{"xid": 13, "timestamp": "2026-02-03T21:39:52Z"}]}]}
    [xid] = [leaf for leaf in leaves(data) if leaf.field == "xid"]
    assert dict(xid.context) == {"node": "n1", "index": "3"} and xid.observed_at is not None


def test_text_check_allows_grounded_numbers():
    assert text_problem("Job exited with 137", {"137"}) is None
    assert text_problem("x" * 400, set()) == "text too long"


def test_text_check_ignores_only_existing_reference_labels():
    labels = frozenset({"T1", "T2", "I1"})
    assert text_problem("T1 and T2 show the node drained (I1)", set(), labels) is None
    # a label that doesn't exist in the run is not exempt: its digits are still checked
    assert text_problem("T7 shows the node drained", set(), labels) == "numbers not found in the data: 7"
    # numbers next to real labels are still checked
    assert text_problem("T1 shows 95 errors", set(), labels) == "numbers not found in the data: 95"
    assert text_problem("T1 shows 95 errors", {"95"}, labels) is None
    # without labels (the default) nothing changes
    assert text_problem("T1 shows the node drained", set()) == "numbers not found in the data: 1"


def test_rules_specialist_is_unchanged_without_a_model(adapter, gateway, scopes):
    run = run_diagnosis(adapter.load_incident("POC-SCHEDULER-001"), gateway, scopes)
    assert all(d.reasoning == "rules" for d in run.diagnoses) and run.triage.reasoning == "rules"


def test_wrapper_keeps_the_rule_agents_identity():
    agent = LLMSpecialist(SchedulerAgent(), Domain.SCHEDULER, FakeLLM({}))
    assert agent.name == "scheduler-agent" and agent.domain is Domain.SCHEDULER


def test_review_shows_reasoning_and_discarded_proposals(tmp_path, adapter, gateway, scopes):
    from hpc_agent.workflows.approval_workflow import ApprovalService, render_review

    llm = FakeLLM({"scheduler": [calls("get_job_status", "get_node_status"), answer(hypotheses=[
        {"cause": "Node drained", "category": "node.unhealthy", "confidence": 0.5, "facts": [DRAIN]},
        {"cause": "Run scontrol to fix it", "category": "node.unhealthy", "facts": [DRAIN]}])]})
    service = ApprovalService(tmp_path / "state", adapter, gateway, scopes, reasoning_llm=llm)
    run = service.diagnose(adapter.load_incident("POC-SCHEDULER-001"))
    review = service.review(run.run_id)
    assert {a.agent: a.reasoning for a in review.agents}["scheduler-agent"] == "llm"
    assert any("contains a command" in p for p in review.discarded_proposals)
    assert "AGENT REASONING" in render_review(review)


@pytest.mark.parametrize("env, expected", [
    ({}, None),
    ({"MODEL_PROVIDER": "ollama", "MODEL_NAME": "qwen2.5:7b"}, None),  # explainer only: llm is opt-in
    ({"MODEL_PROVIDER": "ollama", "MODEL_NAME": "qwen2.5:7b", "HPC_AGENT_REASONING": "llm"}, "qwen2.5:7b"),
    ({"MODEL_PROVIDER": "ollama", "HPC_AGENT_REASONING": "rules"}, None),
])
def test_reasoning_mode_from_environment(monkeypatch, env, expected):
    from hpc_agent.llm.provider import reasoning_llm_from_env

    for key in ("MODEL_PROVIDER", "MODEL_NAME", "HPC_AGENT_REASONING"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    llm = reasoning_llm_from_env()
    assert (llm.model if llm else None) == expected


@pytest.mark.parametrize("env", [{"HPC_AGENT_REASONING": "llm"}, {"HPC_AGENT_REASONING": "magic"},
                                 {"MODEL_PROVIDER": "ollama", "MODEL_NAME": "kimi-k2.5:cloud",
                                  "HPC_AGENT_REASONING": "llm"}])
def test_invalid_reasoning_configuration_is_refused(monkeypatch, env):
    from hpc_agent.llm.provider import LLMConfigError, reasoning_llm_from_env

    for key in ("MODEL_PROVIDER", "MODEL_NAME", "HPC_AGENT_REASONING", "HPC_AGENT_LLM_ALLOW_CLOUD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(LLMConfigError):
        reasoning_llm_from_env()


def test_evaluate_with_llm_reasoning_needs_a_model(monkeypatch, capsys):
    from hpc_agent.main import main

    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert main(["evaluate", "--reasoning", "llm"]) == 2
    assert "MODEL_PROVIDER" in capsys.readouterr().err


def test_llm_evaluation_reports_reasoning_and_discards(tmp_path):
    from hpc_agent.evaluation.harness import evaluate
    from hpc_agent.evaluation.report import render_markdown
    from tests.conftest import PROJECT_ROOT

    cases = tmp_path / "cases"
    cases.mkdir()
    source = PROJECT_ROOT / "evaluation" / "cases" / "POC-SCHEDULER-001.json"
    (cases / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    llm = FakeLLM({"scheduler": [calls("get_job_status", "get_node_status"), answer(hypotheses=[
        {"cause": "Run scontrol", "category": "node.unhealthy", "facts": [DRAIN]}])]})
    results, summary = evaluate(cases, llm=llm)
    assert results[0].reasoning.get("llm", 0) >= 1 and results[0].discarded_proposals == 1
    report = render_markdown(results, summary)
    assert "Agent reasoning (triage + specialists, all cases)" in report
    assert "| Model proposals discarded by the deterministic checks | 1 |" in report


def test_credential_like_model_text_is_never_kept(tmp_path, adapter, gateway, scopes):
    from hpc_agent.workflows.approval_workflow import ApprovalService, render_review

    secret = "password=hunter2"
    llm = FakeLLM({"scheduler": [calls("get_job_status", "get_node_status"), answer(
        finding=f"Login failed with {secret}",
        hypotheses=[{"cause": f"Leaked {secret} in the job", "category": "node.unhealthy", "facts": [DRAIN]},
                    {"cause": "Node drained", "category": "node.unhealthy",
                     "facts": [{"ref": "T2", "field": "state", "value": secret, "where": "sim-node-0042"}]}],
        missing_information=[f"rotate {secret}"], checks=[f"token: {secret}"])]})
    service = ApprovalService(tmp_path / "state", adapter, gateway, scopes, reasoning_llm=llm)
    run = service.diagnose(adapter.load_incident("POC-SCHEDULER-001"))
    d = scheduler(run)
    assert "SENSITIVE INFORMATION DETECTED AND NOT PROCESSED." in " ".join(d.discarded_proposals)
    stored = "".join(p.read_text() for p in (tmp_path / "state").rglob("*") if p.is_file())
    shown = render_review(service.review(run.run_id)) + json.dumps(service.review(run.run_id).model_dump(mode="json"))
    for text in (stored, shown, json.dumps(d.model_dump(mode="json"))):
        assert "hunter2" not in text


def gpu_run(adapter, gateway, scopes, fact):
    llm = FakeLLM({"gpu": [calls("get_gpu_health"), answer(hypotheses=[
        {"cause": "GPU 3 reported an XID event", "category": "gpu.application", "confidence": 0.4, "facts": [fact]}])]})
    run = run_diagnosis(adapter.load_incident("POC-GPU-001"), gateway, scopes, llm=llm)
    return next(d for d in run.diagnoses if d.agent == "gpu-agent"), run


@pytest.mark.parametrize("fact", [
    {"ref": "T1", "field": "xid_events.count", "value": 1, "where": "sim-gpu-node-0007:3"},
    {"ref": "T1", "field": "gpus[3].xid_events[0].xid", "value": 13, "where": "sim-gpu-node-0007/3"},
    {"ref": "T1", "field": "count", "value": 1, "where": "node=sim-gpu-node-0007, index=3"},
    {"ref": "T1", "field": "health", "value": "warn", "where": "index 3"},
])
def test_dotted_paths_and_compound_ids_are_accepted(adapter, gateway, scopes, fact):
    d, run = gpu_run(adapter, gateway, scopes, fact)
    [cause] = d.hypotheses
    [evidence] = cause.supporting_evidence
    assert evidence.statement.startswith("node=sim-gpu-node-0007, index=3: ")
    assert "gpu:sim-gpu-node-0007/3" in cause.components
    verify_run(run, scopes)


@pytest.mark.parametrize("fact", [
    {"ref": "T1", "field": "gpus.count", "value": 1},  # path does not end at the leaf's real path
    {"ref": "T1", "field": "xid_events.count", "value": 1, "where": "sim-gpu-node-0007:2"},  # wrong GPU
    {"ref": "T1", "field": "xid_events.count", "value": 1, "where": "sim-gpu-node-9999:3"},  # wrong node
    {"ref": "T1", "field": "xid_events.count", "value": 2, "where": "sim-gpu-node-0007:3"},  # wrong value
    {"ref": "T1", "field": "", "value": 1},
])
def test_paths_and_ids_still_have_to_match_the_data(adapter, gateway, scopes, fact):
    d, _ = gpu_run(adapter, gateway, scopes, fact)
    assert d.hypotheses == [] and "T1 has no" in d.discarded_proposals[0]


def test_specialists_get_the_site_thresholds(adapter, gateway, scopes):
    from hpc_agent.config import load_site_profile

    profile = load_site_profile("example-pjm-site")
    llm = FakeLLM({"storage": [calls("get_ost_usage"), answer(hypotheses=[])]})
    run_diagnosis(adapter.load_incident("POC-STORAGE-001"), gateway, scopes, profile=profile, llm=llm)
    system = next(messages[0]["content"] for domain, _, messages in llm.seen if domain == "storage")
    assert f"used_pct >= {profile.storage_full_pct:g}" in system
    assert f">= {profile.ost_imbalance_spread_pct:g}" in system and "example-pjm-site" in system


def test_threshold_numbers_may_appear_in_model_text():
    from hpc_agent.agents.llm_agents import site_thresholds
    from hpc_agent.models.site import DEFAULT_PROFILE

    assert "temperature_c >= 85" in site_thresholds(Domain.GPU, DEFAULT_PROFILE)
    assert site_thresholds(Domain.NETWORK, DEFAULT_PROFILE) == "Site thresholds: none for this domain."
