"""Pilot comparison statistics. All numbers here are synthetic test inputs, not results."""

import csv
from datetime import UTC, datetime, timedelta

import pytest

from hpc_agent import main as cli
from hpc_agent.evaluation.pilot import CAVEAT, analyse, load, render

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def write(tmp_path, rows):
    path = tmp_path / "pilot.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["incident_id", "site", "category", "period", "detected_at", "localized_at", "resolved_at"])
        writer.writerows(rows)
    return path


def rows(period, minutes, category="storage", resolve_extra=30):
    return [[f"T-{period}-{i}", "default", category, period, T0.isoformat(),
             (T0 + timedelta(minutes=m)).isoformat(), (T0 + timedelta(minutes=m + resolve_extra)).isoformat()]
            for i, m in enumerate(minutes)]


def result(results, metric="localization", category="all"):
    return next(r for r in results if r.metric == metric and r.category == category)


def test_insufficient_sample_is_not_reported(tmp_path):
    incidents, _ = load(write(tmp_path, rows("baseline", [100] * 9) + rows("assisted", [50] * 12)))
    r = result(analyse(incidents))
    assert r.verdict.startswith("insufficient sample") and r.difference is None


def test_clear_difference_detected(tmp_path):
    base = [110, 120, 125, 130, 118, 140, 122, 135, 128, 119, 131, 126]
    asst = [60, 55, 70, 65, 58, 62, 67, 59, 64, 61, 66, 63]
    incidents, problems = load(write(tmp_path, rows("baseline", base) + rows("assisted", asst)))
    r = result(analyse(incidents))
    assert problems == []
    assert r.verdict == "faster with assistance" and r.difference < 0 and r.ci_high < 0
    assert result(analyse(incidents), "mttr").n_assisted == 12


def test_overlap_is_not_significant(tmp_path):
    base = [60, 120, 90, 45, 150, 75, 100, 80, 130, 70, 95, 110]
    asst = [65, 115, 85, 50, 140, 80, 105, 75, 125, 72, 90, 100]
    incidents, _ = load(write(tmp_path, rows("baseline", base) + rows("assisted", asst)))
    assert result(analyse(incidents)).verdict == "no significant difference"


def test_bad_rows_are_skipped_not_repaired(tmp_path):
    bad = [["X1", "default", "gpu", "during", T0.isoformat(), T0.isoformat(), ""],
           ["X2", "default", "gpu", "baseline", "2026-01-01T00:00:00", T0.isoformat(), ""],
           ["X3", "default", "gpu", "assisted", T0.isoformat(), (T0 - timedelta(minutes=5)).isoformat(), ""]]
    incidents, problems = load(write(tmp_path, bad))
    assert incidents == [] and len(problems) == 3
    assert "row(s) skipped" in render(analyse(incidents), problems)


def test_report_carries_caveat(tmp_path):
    incidents, problems = load(write(tmp_path, rows("baseline", [10] * 10) + rows("assisted", [5] * 10)))
    assert render(analyse(incidents), problems).startswith(CAVEAT)


def test_cli(tmp_path, capsys):
    path = write(tmp_path, rows("baseline", [10] * 3))
    assert cli.main(["compare-pilot", str(path)]) == 0
    assert "insufficient sample" in capsys.readouterr().out
    assert cli.main(["compare-pilot", str(path), "--min-n", "2"]) == 2


def test_template_has_only_the_header():
    from tests.conftest import PROJECT_ROOT

    lines = (PROJECT_ROOT / "evaluation" / "pilot" / "pilot-template.csv").read_text().splitlines()
    assert lines == ["incident_id,site,category,period,detected_at,localized_at,resolved_at"]
