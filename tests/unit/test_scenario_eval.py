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
