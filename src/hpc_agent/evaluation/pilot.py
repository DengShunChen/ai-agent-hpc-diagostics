"""Pilot comparison: fault-localization time and MTTR, baseline vs assisted.

Input is a CSV exported from the ticketing system (template: evaluation/pilot/pilot-template.csv):

    incident_id,site,category,period,detected_at,localized_at,resolved_at
    period = baseline | assisted;  timestamps are ISO 8601 with a timezone

For each category (and overall) it reports the median time per period, the difference,
and a bootstrap 95% confidence interval of the median difference. It refuses to state a
result when either period has fewer than `min_n` incidents, and says "no significant
difference" when the interval includes zero. It never fills in missing data.

This is an observational before/after comparison: incident mix, staffing and seasonality
are not controlled, and the report says so.
"""

from __future__ import annotations

import csv
import random
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PERIODS = ("baseline", "assisted")
CAVEAT = ("Observational before/after comparison: incident mix, staffing and seasonality are not "
          "controlled. Report together with period, site and sample sizes.")


@dataclass(frozen=True)
class Incident:
    incident_id: str
    site: str
    category: str
    period: str
    localization_min: float
    mttr_min: float | None


@dataclass(frozen=True)
class Comparison:
    metric: str
    category: str
    n_baseline: int
    n_assisted: int
    median_baseline: float | None
    median_assisted: float | None
    difference: float | None  # assisted - baseline (negative = faster)
    ci_low: float | None
    ci_high: float | None
    verdict: str


def _time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def load(path: Path) -> tuple[list[Incident], list[str]]:
    """Valid incidents and a list of problems for rows that were skipped (never repaired)."""
    incidents, problems = [], []
    with path.open(newline="", encoding="utf-8") as handle:
        for line, row in enumerate(csv.DictReader(handle), 2):
            detected, localized = _time(row.get("detected_at", "")), _time(row.get("localized_at", ""))
            resolved = _time(row.get("resolved_at", "")) if (row.get("resolved_at") or "").strip() else None
            period = (row.get("period") or "").strip()
            if period not in PERIODS:
                problems.append(f"line {line}: period must be baseline or assisted")
            elif not detected or not localized:
                problems.append(f"line {line}: detected_at and localized_at need timezone-aware ISO times")
            elif localized < detected or (resolved and resolved < detected):
                problems.append(f"line {line}: times go backwards")
            else:
                incidents.append(Incident(
                    incident_id=row.get("incident_id", "").strip(),
                    site=(row.get("site") or "").strip() or "unknown",
                    category=(row.get("category") or "").strip() or "uncategorised",
                    period=period,
                    localization_min=(localized - detected).total_seconds() / 60,
                    mttr_min=(resolved - detected).total_seconds() / 60 if resolved else None,
                ))
    return incidents, problems


def _bootstrap_ci(base: list[float], assisted: list[float], resamples: int, seed: int) -> tuple[float, float]:
    rng = random.Random(seed)
    diffs = sorted(
        statistics.median(rng.choices(assisted, k=len(assisted))) - statistics.median(rng.choices(base, k=len(base)))
        for _ in range(resamples)
    )
    return diffs[int(0.025 * resamples)], diffs[int(0.975 * resamples) - 1]


def compare(values: dict[str, list[float]], metric: str, category: str, min_n: int,
            resamples: int = 2000, seed: int = 7) -> Comparison:
    base, assisted = values.get("baseline", []), values.get("assisted", [])
    med = lambda xs: statistics.median(xs) if xs else None  # noqa: E731
    if len(base) < min_n or len(assisted) < min_n:
        return Comparison(metric, category, len(base), len(assisted), med(base), med(assisted), None, None, None,
                          f"insufficient sample (need at least {min_n} per period)")
    low, high = _bootstrap_ci(base, assisted, resamples, seed)
    difference = statistics.median(assisted) - statistics.median(base)
    if low <= 0 <= high:
        verdict = "no significant difference"
    else:
        verdict = "faster with assistance" if high < 0 else "slower with assistance"
    return Comparison(metric, category, len(base), len(assisted), med(base), med(assisted), difference, low, high,
                      verdict)


def analyse(incidents: Iterable[Incident], min_n: int = 10) -> list[Comparison]:
    incidents = list(incidents)
    results = []
    for category in ["all", *sorted({i.category for i in incidents})]:
        rows = [i for i in incidents if category == "all" or i.category == category]
        for metric, get in (("localization", lambda i: i.localization_min), ("mttr", lambda i: i.mttr_min)):
            values = {p: [v for i in rows if i.period == p and (v := get(i)) is not None] for p in PERIODS}
            results.append(compare(values, metric, category, min_n))
    return results


def render(results: list[Comparison], problems: list[str]) -> str:
    fmt = lambda v: "n/a" if v is None else f"{v:.1f}"  # noqa: E731
    lines = [CAVEAT, "", "metric        category        n(base/asst)  median base  median asst  diff (min)  95% CI          verdict"]
    for r in results:
        ci = "n/a" if r.ci_low is None else f"[{r.ci_low:.1f}, {r.ci_high:.1f}]"
        lines.append(f"{r.metric:<13} {r.category:<15} {r.n_baseline:>4}/{r.n_assisted:<7}  {fmt(r.median_baseline):>11}  "
                     f"{fmt(r.median_assisted):>11}  {fmt(r.difference):>10}  {ci:<15} {r.verdict}")
    if problems:
        lines += ["", f"{len(problems)} row(s) skipped (not repaired):", *[f"  - {p}" for p in problems[:20]]]
    return "\n".join(lines)
