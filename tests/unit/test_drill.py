"""Drill kit: crossover packets without labels, and scoring that reports time together with correctness."""

import csv
from pathlib import Path

import pytest

from hpc_agent.evaluation.drill import DrillError, prepare, score
from hpc_agent.simulate.render import generate
from hpc_agent.simulate.scenario import load_scenarios

DEV = Path(__file__).resolve().parents[2] / "evaluation" / "scenarios" / "dev"


@pytest.fixture(scope="module")
def drill(tmp_path_factory):
    sim = tmp_path_factory.mktemp("sim")
    for sc in [s for s in load_scenarios(DEV) if s.scenario_id in ("SIM-DEV-005", "SIM-DEV-009", "SIM-DEV-011",
                                                                    "SIM-DEV-013")]:
        generate(sc, sim)
    out = tmp_path_factory.mktemp("drill") / "out"
    prepare(sim, out, ["E1", "E2"])
    return sim, out


def rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_crossover_each_scenario_is_assisted_for_exactly_one_group_and_labels_never_leave(drill):
    sim, out = drill
    assignment = rows(out / "assignment.csv")
    for sid in {r["scenario_id"] for r in assignment}:
        assert sorted(r["arm"] for r in assignment if r["scenario_id"] == sid) == ["assisted", "baseline"]
    for code in ("E1", "E2"):
        arms = [r["arm"] for r in assignment if r["engineer"] == code]
        assert arms.count("assisted") == arms.count("baseline") == 2
    assert not any(p.name == "labels" or p.parent.name == "labels" for p in out.rglob("*"))
    for r in assignment:
        folder = next((out / "packets" / r["engineer"]).glob(f"*-{r['scenario_id']}"))
        assert (folder / "raw" / "incident.json").is_file()
        assert (folder / "agent-report.txt").is_file() == (r["arm"] == "assisted")


def test_the_assisted_report_points_at_the_log_lines_on_the_jobs_nodes(drill):
    _, out = drill
    report = (out / "reports" / "SIM-DEV-009.txt").read_text()
    assert "link down" in report and "get_log_digest" in report


def test_output_must_be_new_and_engineers_are_codes(drill, tmp_path):
    sim, out = drill
    with pytest.raises(DrillError, match="new or empty"):
        prepare(sim, out, ["E1", "E2"])
    with pytest.raises(DrillError, match="codes, not names"):
        prepare(sim, tmp_path / "x", ["Jane Doe", "E2"])


def test_score_reports_correctness_with_time_and_skips_bad_rows(drill, tmp_path):
    sim, _ = drill
    sheet = tmp_path / "sheet.csv"
    sheet.write_text(
        "engineer,scenario_id,arm,started_at,answered_at,answer_category,answer_text,confidence\n"
        "E1,SIM-DEV-005,assisted,2026-11-20T09:00:00+08:00,2026-11-20T09:10:00+08:00,node.unhealthy,,\n"
        "E1,SIM-DEV-009,baseline,2026-11-20T09:20:00+08:00,2026-11-20T10:00:00+08:00,application.exit_code,,\n"
        "E1,SIM-DEV-011,assisted,2026-11-20T10:00:00,2026-11-20T10:05:00,storage.capacity,,\n")
    answers, text = score([sheet], sim / "labels", tmp_path / "score")
    assert [(a.scenario_id, a.correct, a.minutes) for a in answers] == [
        ("SIM-DEV-005", True, 10.0), ("SIM-DEV-009", False, 40.0), ("SIM-DEV-011", True, None)]
    assert "timezone" in answers[2].problem
    assert [r["incident_id"] for r in rows(tmp_path / "score" / "drill-results.csv")] == ["E1-SIM-DEV-005", "E1-SIM-DEV-009"]
    assert "| assisted | 1 | 1 (100%) | 10.0 | 10.0 |" in text and "| baseline | 1 | 0 (0%) | 40.0 | – |" in text
