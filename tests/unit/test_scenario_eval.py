"""Scenario evaluation keeps every case: per-case runs, traces and a manifest; failures count as wrong."""

import json

from hpc_agent.evaluation import scenarios
from hpc_agent.evaluation.scenarios import evaluate_scenarios, manifest
from hpc_agent.simulate.render import generate
from hpc_agent.simulate.scenario import Scenario


def make(tmp_path):
    for i, fault in enumerate(("segfault", "walltime"), 1):
        generate(Scenario(scenario_id=f"SIM-E-{i:03d}", seed=900 + i, fault=fault), tmp_path / "sim")
    return tmp_path / "sim"


def test_runs_traces_and_manifest_are_kept(tmp_path):
    directory = make(tmp_path)
    results, summary, logs = evaluate_scenarios(directory, runs_dir=tmp_path / "runs")
    assert summary.cases == 2 and not any(r.error for r in logs)
    record = json.loads((tmp_path / "runs" / "SIM-E-001.run.json").read_text())
    assert record["run"]["incident_id"] == "SIM-E-001" and record["error"] == ""
    assert (tmp_path / "runs" / "SIM-E-002.trace.jsonl").exists()
    m = manifest(directory, None)
    assert m["reasoning"] == "rules" and len(m["code"]["commit"]) >= 7
    assert m["hashes"]["scenario_labels"] != "absent" and "key" not in json.dumps(m).lower()


def test_a_failed_case_is_scored_as_wrong_not_dropped(tmp_path, monkeypatch):
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
    assert failed.leading_correct is False and failed.outcome_correct is False and not failed.classification_correct
    assert next(r for r in logs if r.case_id == "SIM-E-002").error.startswith("TimeoutError")
    assert json.loads((tmp_path / "runs" / "SIM-E-002.run.json").read_text())["run"] is None
