"""Scenario evaluation keeps every planned case: runs, traces, progress and a manifest; failures and fallbacks
are recorded, never dropped or passed off as model results."""

import json
import shutil

import pytest

from hpc_agent.evaluation import scenarios
from hpc_agent.evaluation.scenarios import BatchInvalid, evaluate_scenarios, manifest, render_log_section
from hpc_agent.llm.provider import LLMUnavailableError
from hpc_agent.simulate.render import generate
from hpc_agent.simulate.scenario import Scenario


def make(tmp_path, faults=("segfault", "memory_ecc_node_down")):
    for i, fault in enumerate(faults, 1):
        generate(Scenario(scenario_id=f"SIM-E-{i:03d}", seed=900 + i, fault=fault), tmp_path / "sim")
    return tmp_path / "sim"


def test_runs_traces_progress_and_manifest_are_kept(tmp_path):
    directory = make(tmp_path)
    results, summary, logs = evaluate_scenarios(directory, runs_dir=tmp_path / "runs",
                                                run_manifest=manifest(directory, None))
    assert summary.cases == 2 and not any(r.error for r in logs)
    record = json.loads((tmp_path / "runs" / "SIM-E-001.run.json").read_text())
    assert record["run"]["incident_id"] == "SIM-E-001" and record["error"] == ""
    assert (tmp_path / "runs" / "SIM-E-002.trace.jsonl").exists()
    progress = [json.loads(x) for x in (tmp_path / "runs" / "progress.jsonl").read_text().splitlines()]
    assert [p["status"] for p in progress] == ["completed", "completed"]
    m = json.loads((tmp_path / "runs" / "manifest.json").read_text())
    assert m["status"] == "complete" and m["planned_cases"] == ["SIM-E-001", "SIM-E-002"] and m["reasoning"] == "rules"
    assert m["hashes"]["scenario_labels"] != "absent" and len(m["code"]["commit"]) >= 7
    assert {r.platform for r in logs} == {"slurm-x86"} and {r.group for r in logs} == {"known"}
    assert all(r.model_complete is None for r in logs)  # rules run: no model to complete


def test_runs_directory_must_be_new(tmp_path):
    directory = make(tmp_path)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "old.txt").write_text("x")
    with pytest.raises(ValueError, match="new or empty"):
        evaluate_scenarios(directory, runs_dir=tmp_path / "runs")


def test_a_failed_case_is_kept_with_its_time_and_a_failed_outcome(tmp_path, monkeypatch):
    directory = make(tmp_path)
    real = scenarios.run_diagnosis

    def flaky(incident, *args, **kwargs):
        if incident.incident_id == "SIM-E-002":
            raise TimeoutError("model did not answer")
        return real(incident, *args, **kwargs)

    monkeypatch.setattr(scenarios, "run_diagnosis", flaky)
    results, summary, logs = evaluate_scenarios(directory, runs_dir=tmp_path / "runs")
    assert summary.cases == 2
    failed = next(r for r in results if r.case_id == "SIM-E-002")
    assert failed.outcome == "failed" and not failed.outcome_correct and failed.leading_correct is False
    assert failed.duration_ms >= 0
    log = next(r for r in logs if r.case_id == "SIM-E-002")
    assert log.error.startswith("TimeoutError") and log.component_hit is False  # infra fault: counts as a miss
    assert json.loads((tmp_path / "runs" / "SIM-E-002.run.json").read_text())["run"] is None
    assert "Failed cases" in render_log_section(logs, results)


def test_an_interrupted_batch_is_marked(tmp_path, monkeypatch):
    directory = make(tmp_path)
    monkeypatch.setattr(scenarios, "run_diagnosis", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        evaluate_scenarios(directory, runs_dir=tmp_path / "runs")
    assert json.loads((tmp_path / "runs" / "manifest.json").read_text())["status"] == "interrupted"


def test_labels_and_exports_must_match(tmp_path):
    directory = make(tmp_path)
    shutil.rmtree(directory / "export" / "SIM-E-002")
    with pytest.raises(BatchInvalid, match="labels only: \\['SIM-E-002'\\]"):
        evaluate_scenarios(directory)


def test_an_import_rejection_is_a_failed_case(tmp_path):
    directory = make(tmp_path)
    with (directory / "export" / "SIM-E-002" / "logs" / "syslog").open("a") as handle:
        handle.write("2026-03-02T05:00:00.000000+00:00 sim-node-0001 app: password=hunter2hunter2\n")
    results, summary, logs = evaluate_scenarios(directory)
    assert summary.cases == 2
    log = next(r for r in logs if r.case_id == "SIM-E-002")
    assert log.error.startswith("import rejected") and "hunter2" not in log.error


class DownLLM:
    name, model = "fake", "down"

    def chat(self, messages, tools=None, json_output=False):
        raise LLMUnavailableError("connection refused")


def test_model_failures_are_reported_apart_from_rule_fallbacks(tmp_path):
    directory = make(tmp_path, faults=("segfault",))
    results, summary, logs = evaluate_scenarios(directory, llm=DownLLM())
    [log] = logs
    assert log.model_complete is False and not log.error
    assert any(f.startswith("triage-agent") for f in log.fallbacks)
    assert "Model completion: **0 of 1**" in render_log_section(logs, results)


# --- regression checks for the independent acceptance review (R1-R3) -----------------------------
from hpc_agent.evaluation.report import render_markdown  # noqa: E402
from tests.safety.test_llm_agents import FakeLLM, answer  # noqa: E402


def working_llm():
    return FakeLLM({"triage": [answer(domains=[{"domain": "scheduler", "items": ["I1"]}], primary="scheduler")],
                    "scheduler": [answer(hypotheses=[])]})


def failing_second(monkeypatch, error):
    real = scenarios.run_diagnosis

    def run(incident, *args, **kwargs):
        if incident.incident_id == "SIM-E-002":
            raise error
        return real(incident, *args, **kwargs)

    monkeypatch.setattr(scenarios, "run_diagnosis", run)


def completion(logs, results):
    section = render_log_section(logs, results)
    return section.split("Model completion: **", 1)[1].split("**", 1)[0]


def test_r1_completion_counts_a_timed_out_case(tmp_path, monkeypatch):
    failing_second(monkeypatch, TimeoutError("no answer"))
    results, _, logs = evaluate_scenarios(make(tmp_path), llm=working_llm())
    assert completion(logs, results) == "1 of 2"


def test_r1_completion_counts_an_import_rejection(tmp_path):
    directory = make(tmp_path)
    with (directory / "export" / "SIM-E-002" / "logs" / "syslog").open("a") as handle:
        handle.write("2026-03-02T05:00:00.000000+00:00 sim-node-0001 app: password=hunter2hunter2\n")
    results, _, logs = evaluate_scenarios(directory, llm=working_llm())
    assert completion(logs, results) == "1 of 2"


def test_r1_completion_when_every_case_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(scenarios, "run_diagnosis", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("x")))
    results, _, logs = evaluate_scenarios(make(tmp_path), llm=working_llm())
    assert completion(logs, results) == "0 of 2"


class MeteredLLM:
    """Stands in for the Claude provider's usage counter; never sends anything."""
    name, model = "anthropic", "metered"

    def __init__(self):
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def test_r2_an_interrupted_case_keeps_its_trace_time_and_usage(tmp_path, monkeypatch):
    llm, real = MeteredLLM(), scenarios.run_diagnosis

    def run(incident, gateway, scopes, llm=None, trace=None):
        llm.usage["requests"] += 1
        if incident.incident_id == "SIM-E-002":
            trace.record("scheduler-agent", "model_reply", content="partial")
            raise KeyboardInterrupt()
        return real(incident, gateway, scopes, trace=trace)

    monkeypatch.setattr(scenarios, "run_diagnosis", run)
    with pytest.raises(KeyboardInterrupt):
        evaluate_scenarios(make(tmp_path), llm=llm, runs_dir=tmp_path / "runs")
    runs = tmp_path / "runs"
    record = json.loads((runs / "SIM-E-002.run.json").read_text())
    assert record["status"] == "interrupted" and record["wall_seconds"] >= 0 and record["run"] is None
    assert "partial" in (runs / "SIM-E-002.trace.jsonl").read_text()
    progress = [json.loads(x) for x in (runs / "progress.jsonl").read_text().splitlines()]
    assert [p["status"] for p in progress] == ["completed", "interrupted"]
    assert progress[-1]["claude_usage"]["requests"] == 2  # the latest known usage, not reset
    m = json.loads((runs / "manifest.json").read_text())
    assert m["status"] == "interrupted" and m["active_case"] == "SIM-E-002" and m["pending_cases"] == []


def test_r3_sensitive_exception_text_never_reaches_any_output(tmp_path, monkeypatch):
    failing_second(monkeypatch, RuntimeError("login failed password=hunter2hunter2"))
    results, summary, logs = evaluate_scenarios(make(tmp_path), runs_dir=tmp_path / "runs")
    outputs = [json.dumps([r.model_dump() for r in results]), json.dumps([r.model_dump() for r in logs]),
               render_markdown(results, summary) + render_log_section(logs, results)]
    outputs += [p.read_text() for p in (tmp_path / "runs").iterdir()]
    assert not any("hunter2" in text for text in outputs)
    assert next(r for r in logs if r.case_id == "SIM-E-002").error == \
        "RuntimeError: SENSITIVE INFORMATION DETECTED AND NOT PROCESSED."


def test_r3_ordinary_exception_text_is_kept(tmp_path, monkeypatch):
    failing_second(monkeypatch, TimeoutError("model did not answer within 900 s"))
    _, _, logs = evaluate_scenarios(make(tmp_path))
    assert next(r for r in logs if r.case_id == "SIM-E-002").error == "TimeoutError: model did not answer within 900 s"


class SensitiveDown:
    name, model = "fake", "down"

    def chat(self, messages, tools=None, json_output=False):
        raise LLMUnavailableError("refused: password=hunter2hunter2")


def test_r3_real_fallback_notes_are_masked_in_the_persisted_run(tmp_path):
    results, summary, logs = evaluate_scenarios(make(tmp_path, faults=("segfault",)), llm=SensitiveDown(),
                                                runs_dir=tmp_path / "runs")
    assert logs[0].fallbacks and logs[0].model_complete is False
    texts = [p.read_text() for p in (tmp_path / "runs").iterdir()]
    texts += [json.dumps([r.model_dump() for r in logs]), render_log_section(logs, results)]
    assert not any("hunter2" in t for t in texts)
    run = json.loads((tmp_path / "runs" / "SIM-E-001.run.json").read_text())["run"]
    assert run["triage"]["reasoning"] == "rules-fallback" and "hunter2" not in run["triage"]["reasoning_note"]


def test_r2_interrupt_after_the_run_was_written_closes_the_batch(tmp_path, monkeypatch):
    real = scenarios.score

    def score(case, run):
        if case.case_id == "SIM-E-002":
            raise KeyboardInterrupt()
        return real(case, run)

    monkeypatch.setattr(scenarios, "score", score)
    with pytest.raises(KeyboardInterrupt):  # not FileExistsError
        evaluate_scenarios(make(tmp_path), runs_dir=tmp_path / "runs")
    runs = tmp_path / "runs"
    m = json.loads((runs / "manifest.json").read_text())
    assert m["status"] == "interrupted" and m["active_case"] == "SIM-E-002"
    assert m["active_case_stage"] == "diagnosed, not scored" and m["interruption"] == "KeyboardInterrupt: "
    assert json.loads((runs / "SIM-E-002.run.json").read_text())["status"] == "completed"  # kept as written
    progress = [json.loads(x)["status"] for x in (runs / "progress.jsonl").read_text().splitlines()]
    assert progress == ["completed", "completed", "interrupted after diagnosis"]


def test_a_fallback_in_the_trace_counts_even_if_the_run_record_misses_it(tmp_path, monkeypatch):
    """model_complete is cross-checked against the trace, so a record that loses an agent cannot pass as complete."""
    real = scenarios.run_diagnosis

    def losing_record(*args, trace=None, **kwargs):
        run = real(*args, trace=trace, **kwargs)
        trace.record("network-agent", "fallback", reason="model unavailable")  # an agent the record does not show
        return run

    monkeypatch.setattr(scenarios, "run_diagnosis", losing_record)
    results, summary, logs = evaluate_scenarios(make(tmp_path, faults=("segfault",)), llm=DownLLM())
    [log] = logs
    assert log.model_complete is False
    assert "network-agent: fell back in the trace but not in the run record" in log.fallbacks


def test_a_cascade_run_records_escalation_and_compares_with_the_rules_alone(tmp_path):
    directory = make(tmp_path, faults=("segfault", "walltime"))
    results, summary, logs = evaluate_scenarios(directory, llm=DownLLM(), cascade=True)
    by_fault = {r.fault: r for r in logs}
    kept, handed = by_fault["segfault"], by_fault["walltime"]
    assert kept.escalated is False and kept.model_complete is None and kept.ai_seconds is None  # no model ran
    assert handed.escalated is True and handed.model_complete is False and handed.fallbacks  # model down: recorded
    assert all(r.rules_top1 is not None and r.rules_seconds is not None for r in logs)
    section = render_log_section(logs, results)
    assert "Escalated to the LLM agents: **1 of 2**" in section and "Rules right, cascade **wrong**" in section


def test_a_cascade_needs_a_model(tmp_path):
    with pytest.raises(BatchInvalid, match="cascade"):
        evaluate_scenarios(make(tmp_path, faults=("segfault",)), cascade=True)


def test_a_failed_llm_stage_keeps_the_rules_answer_in_the_cascade_tables(tmp_path, monkeypatch):
    from hpc_agent.tools.gateway import ToolNotAllowedError
    from hpc_agent.workflows import cascade as cascade_module

    real = cascade_module.run_diagnosis

    def budget_spent_in_llm_stage(*args, llm=None, **kwargs):
        if llm is not None:
            raise ToolNotAllowedError("tool call budget exhausted (scheduler-agent -> get_job_status)")
        return real(*args, **kwargs)

    monkeypatch.setattr(cascade_module, "run_diagnosis", budget_spent_in_llm_stage)
    results, _, logs = evaluate_scenarios(make(tmp_path, faults=("walltime",)), llm=DownLLM(), cascade=True)
    (log,), (result,) = logs, results
    assert not log.error and result.outcome != "failed"  # the rules' answer survives
    assert log.escalated is True and log.rules_top1 is not None and log.model_complete is False
    assert any(f.startswith("cascade: LLM stage failed: ToolNotAllowedError") for f in log.fallbacks)
    assert "**1** escalated cases where the model did not finish" in render_log_section(logs, results)
