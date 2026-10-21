# Rolling out to another site

How to bring this platform to another data center operations team (for example, other
sites in East Asia) while keeping diagnosis, safety and metrics comparable across sites.

## What stays standard, what each site configures

| Standard at every site (do not fork) | Configured per site |
|---|---|
| Agents: triage, storage, scheduler, network, GPU, RCA Coordinator, remediation | **Site profile** `config/sites/<site-id>.yaml`: thresholds, call budget, remediation confidence |
| Evidence model, fact/hypothesis/missing-information separation, runtime guards | **Data adapters**: how the site's saved tool data is exported into `test_data/`-style read-only inputs |
| Tool Gateway allowlist, default-deny policy, human approval, hash-chained audit | **Knowledge base**: the site's own de-identified SOPs, manuals and past incidents in `knowledge/` |
| Evaluation metrics and their definitions; workflow metrics (`hpc-agent metrics`) | **Operator tokens**: issued per site; token files never leave the site |
| Container image, built from the same hash-locked lockfile | **Agent tool scopes** (`config/agents.yaml`): may be narrowed, never widened |

A site profile can only tune *diagnostic thresholds*. It has no fields for the safety
policy, tools, approval rules or execution. Unknown fields are rejected, every value is
bounded, the tool-call budget is capped at 200, and the remediation confidence can't go
below 0.3. Tests enforce all of this (`tests/unit/test_site_profile.py`).

## Onboarding checklist for a new site

1. **Security review.** Do this before any real data is involved. The review covers data
   classification, network placement, who receives tokens, and where `audit-data/` lives.
2. **Site profile.**
   - Copy `config/sites/default.yaml` to `config/sites/<site-id>.yaml`.
   - Set `scheduler: slurm | pjm` and the thresholds the site's engineers agree on: full
     storage percentage, OST imbalance spread, GPU thermal limit, correlation window and
     remediation confidence.
   - Run with `HPC_AGENT_SITE=<site-id>`.
3. **Data adapters, read-only.**
   - Export job and node status, filesystem and OST usage, interface status and
     DCGM-style GPU health into the saved-data formats in `test_data/`.
   - Keep the exports read-only, and keep identifiers de-identified until a separate
     review approves otherwise.
4. **Knowledge base.** Add the site's SOPs (`SOP-*`), manuals (`MAN-*`) and resolved
   incidents (`SIM-HIST-*` or the site's own IDs) as Markdown. The loader rejects
   credential-like content.
5. **Evaluation cases.**
   - Add 10 to 20 labelled cases from the site's past incidents to `evaluation/cases/`.
     They should be labelled by the site's engineers, not by the platform developers.
   - Run `hpc-agent evaluate`. Domain classification, refusal accuracy and the
     unsupported-claim rate must meet the agreed bar before go-live.
6. **Deploy.**
   - Build the image from the same lockfile.
   - Run it with `scripts/podman_run.sh`: rootless, read-only, localhost-bound, behind
     the site's authenticating TLS proxy.
   - Run `scripts/container_smoke_test.sh`.
7. **Operators.** Issue tokens with `create-operator-token`, store them outside the
   repository, and walk the team through `docs/demo.md`.
8. **Measure.** Collect `hpc-agent metrics` monthly, and follow
   [business-impact.md](business-impact.md) for the before/after comparison.

## Keeping sites comparable

- Every run records its `site`, and `hpc-agent metrics` groups results by site, so a
  site's numbers aren't mixed with another's.
- Metric definitions live in code (`metrics.py`, `evaluation/harness.py`), not in each
  site's spreadsheets.
- Changes to agents or the playbook go through one repository, and each site upgrades by
  image tag. A site-specific fork would break comparability.

## Localization

The review page is in Traditional Chinese. Its labels sit in one table in
`api/static/app.js`, and server values (status, domain, risk) keep their English codes, so
adding Japanese or Korean labels for another site is a contained change. Agent output and
evidence are currently in English.
