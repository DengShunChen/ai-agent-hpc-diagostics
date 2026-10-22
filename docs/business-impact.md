# Business impact: measuring fault-localization time and MTTR

The goal is to shorten fault-localization time and, through it, MTTR. **No reduction is
claimed yet.** This document defines how to measure it honestly.

## Where the platform can save time

```
detect -> triage -> collect evidence -> correlate -> localize -> decide -> remediate -> verify
          |_______________ platform-assisted ____________|   |human|   (outside the platform)
```

The platform aims at the middle of that chain:

- automatic routing to the right specialists;
- evidence collection through allowlisted read-only tools;
- cross-domain correlation and a timeline;
- ranked hypotheses with explicit missing information and checks;
- SOP and past-incident references;
- a remediation proposal ready for human approval.

Detection, the actual remediation work and verification stay outside the platform, so
MTTR improvement can only be partial. How large it is must be measured, not assumed.

These are **hypotheses to test**, not results:

1. Evidence collection across storage, scheduler, network and GPU, which is manual and
   sequential today, can become a single run.
2. Cross-domain incidents benefit most, for example a drained node plus SIGKILL plus a
   link going down.
3. Explicit missing information shortens the "what should I check next" loop.

## What the platform already measures

`hpc-agent metrics` (CLI) or `GET /api/metrics` (API) reads the platform's own records,
grouped by site:

| Metric | Definition | Status |
|---|---|---|
| Diagnosis time | Run start to diagnosis ready | Measured (milliseconds today; the agents are deterministic) |
| Report to diagnosis | Incident timestamp to diagnosis ready | Measured when the incident has a timestamp |
| Review time | Awaiting approval to the human decision (median, p90) | Measured from real decisions only |
| Human override rate | Rejected or revision-requested decisions over all decisions | Measured from real decisions only |
| Unsupported-claim rate | From `hpc-agent evaluate` | 0% on 13 synthetic cases |

These are workflow timings, **not MTTR**.

## Measurement protocol (pilot)

1. **Baseline (before).**
   - For 4 to 8 weeks, record from the ticketing system for each incident: detection,
     start of triage, time the root cause was localized, remediation start, and resolution.
   - Record the incident category: storage, scheduler, network, GPU or cross-domain.
2. **Assisted period (after).**
   - The same team handles the same categories with the platform in the loop.
   - Record the same timestamps, plus the platform's run ID so the two records can be joined.
3. **Primary outcome: time to localize the root cause.** This is the part the platform
   influences directly. **Secondary outcome: MTTR.**
4. **Analysis.** Export the tickets with the header in `evaluation/pilot/pilot-template.csv`,
   then run `hpc-agent compare-pilot <file>.csv`. It reports median localization time and
   MTTR per category and period, with a bootstrap 95% CI of the difference. It refuses to
   report a category with fewer than 10 incidents per period.
   - Compare medians per category, with bootstrapped confidence intervals.
   - Report sample sizes, and report "not significant" when it isn't.
   - Don't pool sites with different profiles without saying so.
5. **Guardrails that must not regress:**
   - unsupported-claim rate;
   - human override rate (a rising rate means the proposals are getting worse);
   - no executed action without approval;
   - audit chains verify.
6. **Reporting rule.** Report MTTR or localization-time changes only from this protocol,
   with the period, site, sample size and interval stated.

The operational procedure for running this protocol at a site is in
[pilot-runbook.md](pilot-runbook.md) (Traditional Chinese).

## What is needed before any claim

- Representative incidents: real, de-identified, labelled by site engineers.
- Read-only production data adapters (separate security review).
- A baseline from the ticketing system.
- For localization time on non-trivial incidents, a model with real reasoning capacity.
  Today's deterministic agents plus a 3B explainer are a safety-first foundation, not the
  final reasoning engine (see the README's known limitations).
