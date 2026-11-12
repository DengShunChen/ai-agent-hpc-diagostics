"""Cascade: the rules first; the LLM agents only where the rules lead with how a job ended, never over a refusal."""

import pytest

from hpc_agent.models import HypothesisStatus
from hpc_agent.runtime.definitions import DefinitionError, load_definitions, outcome_categories, parse_definition
from hpc_agent.safety.guards import verify_run
from hpc_agent.workflows.cascade import run_cascade
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis
from tests.safety.test_llm_agents import DRAIN, EXIT, FakeLLM, answer, calls

TRIAGE = [answer(domains=[{"domain": "scheduler", "items": ["I1"]}], primary="scheduler")]


def scheduler_llm(hypotheses):
    return FakeLLM({"triage": TRIAGE,
                    "scheduler": [calls("get_job_status", "get_node_status"), answer(hypotheses=hypotheses)]})


def leading(run):
    return [c.category for c in run.rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]


def test_outcome_categories_come_from_the_agent_definitions():
    assert outcome_categories(load_definitions()) == {"scheduler.signal_termination", "scheduler.walltime",
                                                      "application.exit_code", "gpu.unexplained"}


def test_an_outcome_category_must_be_one_of_the_agents_categories():
    text = ("---\nname: x-agent\ndescription: d\ndomain: scheduler\nllm:\n  categories: [scheduler.walltime]\n"
            "outcome_categories: [node.unhealthy]\n---\nprompt\n")
    with pytest.raises(DefinitionError, match="outcome_categories"):
        parse_definition(text, "x-agent.md")


@pytest.mark.parametrize("incident", ["POC-GPU-001", "POC-STORAGE-001"])
def test_a_cause_or_a_refusal_from_the_rules_is_kept_and_no_model_runs(adapter, gateway, scopes, incident):
    llm = FakeLLM({})
    run = run_cascade(adapter.load_incident(incident), gateway, llm, scopes)
    assert llm.seen == [] and run.cascade.escalated is False and run.cascade.ai_seconds is None
    rules = run_diagnosis(adapter.load_incident(incident), gateway, scopes)
    assert leading(run) == leading(rules) and all(d.reasoning == "rules" for d in run.diagnoses)


def test_a_checked_cause_from_the_llm_agents_leads_and_the_rules_stay_in_the_top_three(adapter, gateway, scopes):
    llm = scheduler_llm([{"cause": "The node was drained", "category": "node.unhealthy", "confidence": 0.3,
                          "facts": [DRAIN]}])
    incident = adapter.load_incident("POC-SCHEDULER-001")
    rules_lead = leading(run_diagnosis(incident, gateway, scopes))[0]
    run = run_cascade(incident, gateway, llm, scopes)
    assert run.cascade.escalated and run.cascade.rules_lead == rules_lead == "scheduler.signal_termination"
    assert leading(run)[0] == run.cascade.ai_lead == "node.unhealthy"  # leads despite a lower confidence
    assert rules_lead in leading(run)[:3]
    assert {d.reasoning for d in run.diagnoses} == {"llm", "rules"}  # both runs' records are kept
    verify_run(run, scopes)  # every fact traces to a call in this record


def test_an_outcome_from_the_llm_agents_does_not_replace_the_rules(adapter, gateway, scopes):
    llm = scheduler_llm([{"cause": "Killed by a signal", "category": "scheduler.signal_termination",
                          "confidence": 0.9, "facts": [EXIT]}])
    incident = adapter.load_incident("POC-SCHEDULER-001")
    run = run_cascade(incident, gateway, llm, scopes)
    assert run.cascade.escalated and run.cascade.ai_lead is None and "no checked cause" in run.cascade.reason
    assert run.rca == run_diagnosis(incident, gateway, scopes).rca.model_copy(update={"incident_id": run.incident_id})
    verify_run(run, scopes)


def test_when_the_model_fails_the_rules_result_is_kept_and_the_fallbacks_are_recorded(adapter, gateway, scopes):
    from hpc_agent.llm.provider import LLMUnavailableError

    llm = FakeLLM({"triage": [LLMUnavailableError("down")], "scheduler": [LLMUnavailableError("down")]})
    incident = adapter.load_incident("POC-SCHEDULER-001")
    run = run_cascade(incident, gateway, llm, scopes)
    assert run.cascade.escalated and run.cascade.ai_lead is None
    assert leading(run) == leading(run_diagnosis(incident, gateway, scopes))
    assert any(d.reasoning == "rules-fallback" for d in run.diagnoses)  # evaluation counts the model as incomplete
    verify_run(run, scopes)
