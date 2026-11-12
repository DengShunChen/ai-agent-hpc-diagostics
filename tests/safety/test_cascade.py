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


def test_the_rules_lead_stays_first_and_a_checked_llm_cause_comes_right_below(adapter, gateway, scopes):
    """A wrong but checked model cause must never displace a correct rules answer (3 did in the first 7B dev run)."""
    llm = scheduler_llm([{"cause": "The node was drained", "category": "node.unhealthy", "confidence": 0.9,
                          "facts": [DRAIN]}])
    incident = adapter.load_incident("POC-SCHEDULER-001")
    rules_lead = leading(run_diagnosis(incident, gateway, scopes))[0]
    run = run_cascade(incident, gateway, llm, scopes)
    assert run.cascade.escalated and run.cascade.rules_lead == rules_lead == "scheduler.signal_termination"
    assert leading(run)[:2] == [rules_lead, "node.unhealthy"]  # second despite a higher confidence
    assert run.cascade.ai_lead == "node.unhealthy"
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


def test_a_failed_llm_stage_keeps_the_rules_result_and_says_the_model_did_not_finish(adapter, gateway, scopes,
                                                                                     monkeypatch):
    from hpc_agent.tools.gateway import ToolNotAllowedError
    from hpc_agent.workflows import cascade as cascade_module

    real = cascade_module.run_diagnosis

    def spent(*args, llm=None, **kwargs):
        if llm is not None:
            raise ToolNotAllowedError("tool call budget exhausted (scheduler-agent -> get_job_status)")
        return real(*args, **kwargs)

    monkeypatch.setattr(cascade_module, "run_diagnosis", spent)
    incident = adapter.load_incident("POC-SCHEDULER-001")
    run = run_cascade(incident, gateway, FakeLLM({}), scopes)
    assert run.cascade.escalated and run.cascade.ai_error.startswith("ToolNotAllowedError")
    assert leading(run) == leading(run_diagnosis(incident, gateway, scopes)) and run.cascade.ai_lead is None
    verify_run(run, scopes)


def test_the_second_opinion_is_what_the_report_shows_second_even_when_another_agent_fell_back(tmp_path, scopes):
    """An LLM-stage rules fallback repeats the rules' causes; they must not take the second place (acceptance-5 R2)."""
    import json

    from hpc_agent.llm.provider import LLMUnavailableError
    from hpc_agent.tools.gateway import build_default_gateway
    from tests.conftest import PROJECT_ROOT, make_data_root

    incident = json.loads((PROJECT_ROOT / "fixtures/incidents/POC-SCHEDULER-001.json").read_text())
    incident |= {"incident_id": "SIM-R2-001", "symptoms": [*incident["symptoms"], "GPU job on sim-gpu-node-0007"]}
    saved = {f"scheduler/{n}": json.loads((PROJECT_ROOT / f"test_data/scheduler/POC-SCHEDULER-001/{n}.json").read_text())
             for n in ("job_status", "node_status")}
    saved["gpu/gpu_health"] = {"node": "sim-gpu-node-0007",
                               "gpus": [{"index": 0, "health": "pass", "xid_events": [], "pcie_replay_count": 5000}]}
    adapter = make_data_root(tmp_path, incident, saved)
    triage = [answer(domains=[{"domain": "scheduler", "items": ["I1"]}, {"domain": "gpu", "items": ["I3"]}],
                     primary="scheduler")]
    replay = {"ref": "T1", "field": "pcie_replay_count", "value": 5000, "where": "0"}
    llm = FakeLLM({"triage": triage, "scheduler": [LLMUnavailableError("down")],  # falls back to the rules
                   "gpu": [calls("get_gpu_health"), answer(hypotheses=[{
                       "cause": "PCIe replays on GPU 0", "category": "gpu.interconnect", "confidence": 0.4,
                       "facts": [replay]}])]})
    run = run_cascade(adapter.load_incident("SIM-R2-001"), build_default_gateway(adapter), llm, scopes)
    shown = [(c.category, c.cause) for c in run.rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]
    assert len(shown) == len(set(shown))  # the fallback's repeat of the rules' lead is not ranked again
    assert shown[0][0] == "scheduler.signal_termination" and shown[1][0] == run.cascade.ai_lead == "gpu.interconnect"
    assert {d.reasoning for d in run.diagnoses} == {"llm", "rules", "rules-fallback"}  # the record keeps all of them
    verify_run(run, scopes)


@pytest.mark.parametrize("where", ["final_guard", "merge"])
def test_a_failure_while_merging_or_verifying_keeps_the_rules_result(adapter, gateway, scopes, monkeypatch, where):
    """Any failure after the rules answered, not only in the LLM run itself (acceptance-6 R1b)."""
    from hpc_agent.safety.guards import SafetyViolation
    from hpc_agent.workflows import cascade as cascade_module

    real_correlate = cascade_module.RCACoordinator.correlate

    def boom(*_args, **_kwargs):
        raise SafetyViolation("injected")

    def merge_fails(self, *args, rank=None, **kwargs):  # only the cascade's merge passes a rank
        if rank is not None:
            raise RuntimeError("injected")
        return real_correlate(self, *args, **kwargs)

    if where == "final_guard":
        monkeypatch.setattr(cascade_module, "verify_run", boom)  # only the cascade's own final check
    else:
        monkeypatch.setattr(cascade_module.RCACoordinator, "correlate", merge_fails)
    llm = scheduler_llm([{"cause": "The node was drained", "category": "node.unhealthy", "confidence": 0.3,
                          "facts": [DRAIN]}])
    incident = adapter.load_incident("POC-SCHEDULER-001")
    run = run_cascade(incident, gateway, llm, scopes)
    assert run.cascade.escalated and run.cascade.ai_error.endswith("injected") and run.cascade.ai_lead is None
    assert leading(run) == leading(run_diagnosis(incident, gateway, scopes))
    assert all(d.reasoning == "rules" for d in run.diagnoses)  # nothing from the LLM stage is kept
    verify_run(run, scopes)


def test_an_interrupt_during_the_llm_stage_is_not_swallowed(adapter, gateway, scopes, monkeypatch):
    from hpc_agent.workflows import cascade as cascade_module

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cascade_module, "verify_run", interrupted)
    llm = scheduler_llm([{"cause": "The node was drained", "category": "node.unhealthy", "confidence": 0.3,
                          "facts": [DRAIN]}])
    with pytest.raises(KeyboardInterrupt):
        run_cascade(adapter.load_incident("POC-SCHEDULER-001"), gateway, llm, scopes)
