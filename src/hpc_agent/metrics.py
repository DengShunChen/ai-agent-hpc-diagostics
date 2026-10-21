"""Operational workflow metrics from recorded runs and human decisions.

These are the segments of incident handling this platform touches, measured from its own
records (run snapshots and the hash-chained approval log):

    reported -> diagnosis ready      (report_to_diagnosis)  needs the incident timestamp
    diagnosis start -> ready         (diagnosis)
    awaiting approval -> decision    (review)               human review latency
    decision -> closed               (close)

They are NOT MTTR. MTTR also covers detection, remediation and verification, which happen
outside this system; measuring it needs incident resolution times from the ticketing
system and a baseline period (see docs/business-impact.md). Nothing is simulated: runs
without a human decision simply have no review time.
"""

from __future__ import annotations

import statistics
from datetime import datetime

from pydantic import BaseModel

from hpc_agent.models import ApprovalStatus as S

NOT_MTTR = ("Workflow timings from this platform's own records. They are not MTTR: detection, "
            "remediation and verification happen outside the platform.")


class RunTiming(BaseModel):
    run_id: str
    incident_id: str
    site: str
    status: str
    decision: str | None
    report_to_diagnosis_s: float | None
    diagnosis_s: float
    review_s: float | None
    decision_to_close_s: float | None


class SiteMetrics(BaseModel):
    site: str
    runs: int
    awaiting_decision: int
    decided: int
    approved: int
    rejected: int
    revision_requested: int
    human_override_rate: float | None
    diagnosis_s_median: float | None
    review_s_median: float | None
    review_s_p90: float | None
    report_to_diagnosis_s_median: float | None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    return (end - start).total_seconds() if start and end else None


def collect(service) -> list[RunTiming]:
    timings = []
    for latest in service.overview():
        run = service.runs.load(latest.run_id)
        history = service.history(latest.run_id)
        at = {r.status: r.timestamp for r in history}
        decision = next((r for r in history if r.status in (S.APPROVED, S.REJECTED, S.REVISION_REQUESTED)), None)
        timings.append(RunTiming(
            run_id=run.run_id,
            incident_id=run.incident_id,
            site=run.site,
            status=latest.status,
            decision=decision.status if decision else None,
            report_to_diagnosis_s=_seconds(run.incident_reported_at, run.end_time),
            diagnosis_s=(run.end_time - run.start_time).total_seconds(),
            review_s=_seconds(at.get(S.AWAITING_HUMAN_APPROVAL), decision.timestamp if decision else None),
            decision_to_close_s=_seconds(decision.timestamp if decision else None, at.get(S.CLOSED)),
        ))
    return timings


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.quantiles(values, n=10, method="inclusive")[-1] if len(values) > 1 else values[0]


def summarize(timings: list[RunTiming]) -> list[SiteMetrics]:
    result = []
    for site in sorted({t.site for t in timings}):
        rows = [t for t in timings if t.site == site]
        decided = [t for t in rows if t.decision]
        overrides = sum(t.decision in (S.REJECTED, S.REVISION_REQUESTED) for t in decided)
        result.append(SiteMetrics(
            site=site,
            runs=len(rows),
            awaiting_decision=sum(t.status == S.AWAITING_HUMAN_APPROVAL for t in rows),
            decided=len(decided),
            approved=sum(t.decision == S.APPROVED for t in decided),
            rejected=sum(t.decision == S.REJECTED for t in decided),
            revision_requested=sum(t.decision == S.REVISION_REQUESTED for t in decided),
            human_override_rate=overrides / len(decided) if decided else None,
            diagnosis_s_median=_median([t.diagnosis_s for t in rows]),
            review_s_median=_median([t.review_s for t in decided if t.review_s is not None]),
            review_s_p90=_p90([t.review_s for t in decided if t.review_s is not None]),
            report_to_diagnosis_s_median=_median(
                [t.report_to_diagnosis_s for t in rows if t.report_to_diagnosis_s is not None]),
        ))
    return result


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 120:
        return f"{seconds:.1f} s"
    if seconds < 7200:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def render(metrics: list[SiteMetrics]) -> str:
    lines = [NOT_MTTR, ""]
    if not metrics:
        return "\n".join(lines + ["No recorded runs."])
    for m in metrics:
        override = "n/a (no decisions yet)" if m.human_override_rate is None else f"{m.human_override_rate:.0%}"
        lines += [
            f"Site {m.site}: {m.runs} run(s), {m.awaiting_decision} awaiting decision, {m.decided} decided "
            f"({m.approved} approved, {m.rejected} rejected, {m.revision_requested} revision requested)",
            f"  human override rate:            {override}",
            f"  diagnosis time (median):        {_fmt(m.diagnosis_s_median)}",
            f"  review time to decision:        median {_fmt(m.review_s_median)}, p90 {_fmt(m.review_s_p90)}",
            f"  report -> diagnosis ready:      {_fmt(m.report_to_diagnosis_s_median)}"
            + ("  (synthetic fixtures carry old timestamps)" if m.report_to_diagnosis_s_median else ""),
        ]
    return "\n".join(lines)
