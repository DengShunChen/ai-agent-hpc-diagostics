# Evaluation cases (synthetic)

One JSON file per case. A case either references a POC fixture (`"fixture"`) or carries an
inline `incident` plus saved `test_data` (keys `domain/dataset`). `expected` holds the label:

- `classification`: acceptable RCA classifications
- `outcome`: `supported_hypothesis` (leading hypothesis backed by tool evidence),
  `insufficient_evidence` (hypotheses only from reported symptoms), or
  `no_supported_hypothesis` (nothing open)
- `leading_categories`: acceptable categories for the top-ranked open hypothesis
- `forbidden_categories`: categories that must not appear as open hypotheses
- `required_missing`: substrings that must appear in the missing-information list

**Post-hoc change (2026-10-21):** after the first run, the RCA classification rule was changed
because of EVAL-STOR-001 and EVAL-STOR-003. Classification accuracy before the change was
85% (11/13). Later results on these cases don't independently test that rule. The disclosure
is repeated in every generated report (`evaluation/report.py`, `POST_HOC_CHANGES`).

**Limitation:** these labels were written by the developer of the agents. They check that the
system behaves as designed; they are not an independent accuracy benchmark. Representative,
de-identified incidents labelled by operations engineers are needed for that.
