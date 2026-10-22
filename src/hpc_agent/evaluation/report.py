"""Markdown evaluation report and scenario comparison."""

from __future__ import annotations

from datetime import UTC, datetime

from hpc_agent.evaluation.harness import CaseResult, Summary

POC_CASES = ("POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001")

# Changes made to the system after seeing results on this case set. They stay in every
# generated report: results on these cases cannot count as an independent test of them.
POST_HOC_CHANGES = [
    "**2026-10-21: RCA classification rule.** The first evaluation run classified EVAL-STOR-001 and "
    "EVAL-STOR-003 as `multi-domain`. Each is a single-symptom write failure (\"job failed while "
    "writing…\") where keyword triage tied storage with the scheduler, although only storage had "
    "tool evidence. Domain classification was then **85% (11/13)**. The rule was changed so that "
    "when tool-backed hypotheses span exactly one domain, the RCA uses that domain. Classification "
    "results on these 13 cases after that change are **not** an independent test of the rule; new, "
    "independently labelled cases are needed for that.",
]


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _mark(value: bool | None) -> str:
    return "–" if value is None else ("yes" if value else "**no**")


def is_synthetic(label_source: str) -> bool:
    return label_source.strip().lower().startswith("synthetic")


def _header(results: list[CaseResult], generated: str) -> list[str]:
    """Title and caveat, worded from the cases' own label_source (never assumed)."""
    sources: dict[str, list[str]] = {}
    for r in results:
        sources.setdefault(r.label_source, []).append(r.case_id)
    synthetic = sum(1 for r in results if is_synthetic(r.label_source))
    if results and synthetic == len(results):
        return [
            "# POC Evaluation Report",
            "",
            f"Generated {generated} by `hpc-agent evaluate` over {len(results)} synthetic cases.",
            "",
            "> **Read this first.** All cases are synthetic, and their labels were written by the developer",
            "> of the agents. The numbers show that the system behaves as designed on designed examples;",
            "> they are **not** evidence of accuracy on real incidents. The agents are deterministic, so",
            "> processing times say nothing about LLM latency. **No MTTR reduction is claimed.**",
        ]
    mix = (f"{synthetic} of {len(results)} cases are synthetic; results on them say nothing about real "
           "incidents. " if synthetic else "")
    return [
        "# Evaluation Report",
        "",
        f"Generated {generated} by `hpc-agent evaluate` over {len(results)} cases.",
        "",
        f"> **Read this first.** {mix}Labels come from the sources listed below, as stated in each case",
        "> file; check who labelled them before relying on the numbers. Results apply to these cases only.",
        "> The agents are deterministic, so processing times say nothing about LLM latency.",
        "> **No MTTR reduction is claimed**; that needs the pilot protocol in docs/business-impact.md.",
        "",
        "## Case sources",
        "",
        "| Label source | Cases |",
        "|---|---|",
        *[f"| {' '.join(source.split()).replace('|', '/')} | {len(ids)}: {', '.join(ids)} |" for source, ids in sources.items()],
    ]


def render_markdown(results: list[CaseResult], summary: Summary) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    override = (f"{_pct(summary.human_override_rate)} of {summary.human_decisions} recorded decisions"
                if summary.human_override_rate is not None
                else "not measured: no recorded human decisions were supplied")
    lines = [
        *_header(results, generated),
        "",
        "## Disclosure: changes made after seeing these results",
        "",
        *[f"- {change}" for change in POST_HOC_CHANGES],
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Correct domain classification | {_pct(summary.classification_accuracy)} |",
        f"| Root cause accuracy, leading hypothesis (top-1) | {_pct(summary.top1_root_cause_accuracy)} |",
        f"| Root cause accuracy, top-3 | {_pct(summary.top3_root_cause_accuracy)} |",
        f"| Outcome matches label (supported / insufficient / none) | {_pct(summary.outcome_accuracy)} |",
        f"| Correct refusal to conclude (cases labelled insufficient or none) | {_pct(summary.refusal_accuracy)} |",
        f"| Evidence quality: open hypotheses backed by tool evidence | {_pct(summary.evidence_backed_hypothesis_rate)} |",
        f"| Evidence quality: required missing information identified | {_pct(summary.missing_information_recall)} |",
        f"| Unsupported claim rate (per open hypothesis) | {_pct(summary.unsupported_claim_rate)} ({summary.unsupported_claims} claims) |",
        f"| Human override rate | {override} |",
        f"| Diagnostic processing time (mean / median / max) | {summary.processing_ms_mean:.0f} / "
        f"{summary.processing_ms_median:.0f} / {summary.processing_ms_max:.0f} ms |",
        "",
        "## Per-case results",
        "",
        "| Case | Classification | Outcome (expected) | Leading hypothesis | Conf. | Top-1 | Tool-backed | Unsupported | Missing found | ms |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        outcome = r.outcome if r.outcome_correct else f"**{r.outcome}** ({r.expected_outcome})"
        cls = r.classification if r.classification_correct else f"**{r.classification}**"
        lines.append(
            f"| {r.case_id} | {cls} | {outcome} | {r.leading_category or '–'} | {r.leading_confidence:.2f} | "
            f"{_mark(r.leading_correct)} | {r.tool_backed_hypotheses}/{r.open_hypotheses} | "
            f"{len(r.unsupported_claims)} | {r.missing_found}/{r.missing_required} | {r.duration_ms:.0f} |"
        )

    poc = [r for r in results if r.case_id in POC_CASES]
    if poc:
        lines += [
            "",
            "## Scenario comparison (POC scenarios)",
            "",
            "| | " + " | ".join(r.case_id for r in poc) + " |",
            "|---|" + "---|" * len(poc),
            "| Classification | " + " | ".join(r.classification for r in poc) + " |",
            "| Outcome | " + " | ".join(r.outcome.replace("_", " ") for r in poc) + " |",
            "| Leading hypothesis | " + " | ".join(r.leading_cause or "none" for r in poc) + " |",
            "| Leading confidence | " + " | ".join(f"{r.leading_confidence:.2f}" for r in poc) + " |",
            "| Open hypotheses (tool-backed) | " + " | ".join(
                f"{r.open_hypotheses} ({r.tool_backed_hypotheses})" for r in poc) + " |",
            "| Facts: reported / tool | " + " | ".join(f"{r.facts_reported} / {r.facts_tool}" for r in poc) + " |",
            "| Knowledge references | " + " | ".join(str(r.references) for r in poc) + " |",
            "| Proposed actions | " + " | ".join(str(r.proposed_actions) for r in poc) + " |",
            "| Root cause confirmed | " + " | ".join("no" for _ in poc) + " |",
        ]

    flagged = [(r.case_id, c) for r in results for c in r.unsupported_claims]
    lines += ["", "## Unsupported claims", ""]
    lines += [f"- {case}: {claim}" for case, claim in flagged] or ["None detected."]
    misses = [r for r in results if not (r.classification_correct and r.outcome_correct and r.leading_correct is not False)]
    lines += ["", "## Mismatches against labels", ""]
    lines += [f"- {r.case_id}: classification={r.classification}, outcome={r.outcome} "
              f"(expected {r.expected_outcome}), leading={r.leading_category}" for r in misses] or ["None."]
    lines += [
        "",
        "## Method",
        "",
        "- Cases live in `evaluation/cases/`. Each case runs through the full workflow (triage,",
        "  specialists, RCA Coordinator, runtime guard) in an isolated temporary data root.",
        "- **Outcome** is `supported_hypothesis` when the leading open hypothesis has tool evidence,",
        "  `insufficient_evidence` when it rests only on reported symptoms, and",
        "  `no_supported_hypothesis` when nothing is open.",
        "- **Unsupported claims** are confirmed causes, open hypotheses with confidence of at least",
        "  0.5 and no tool evidence, and hypotheses in a case's forbidden categories.",
        "- **Human override rate** counts REJECTED and REVISION_REQUESTED among recorded human",
        "  decisions (`--approvals-log`). It is never simulated.",
        "- **Next step toward a real evaluation:** representative, de-identified incidents labelled",
        "  independently by operations engineers, plus override rates from real reviews.",
    ]
    return "\n".join(lines) + "\n"
