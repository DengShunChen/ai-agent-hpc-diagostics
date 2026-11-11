# POC Evaluation Report

Generated 2026-11-11 02:24 UTC by `hpc-agent evaluate` over 13 synthetic cases.

> **Read this first.** All cases are synthetic, and their labels were written by the developer
> of the agents. The numbers show that the system behaves as designed on designed examples;
> they are **not** evidence of accuracy on real incidents.
> Triage and the specialist agents were driven by a language model (local, or a cloud API when configured) whose proposals passed deterministic checks; processing times include model inference or API round trips. **No MTTR reduction is claimed.**

## Disclosure: changes made after seeing these results

- **2026-10-21: RCA classification rule.** The first evaluation run classified EVAL-STOR-001 and EVAL-STOR-003 as `multi-domain`. Each is a single-symptom write failure ("job failed while writing…") where keyword triage tied storage with the scheduler, although only storage had tool evidence. Domain classification was then **85% (11/13)**. The rule was changed so that when tool-backed hypotheses span exactly one domain, the RCA uses that domain. Classification results on these 13 cases after that change are **not** an independent test of the rule; new, independently labelled cases are needed for that.
- **2026-10-22: LLM specialist prompt and fact parsing.** After one qwen2.5:3b run on POC-SCHEDULER-001 (one of these cases), in which the model called only knowledge tools and put incident items (I#) under tool facts, the prompt was changed to name the agent's data tools and ask for them first, and an I# under facts is now treated as a reported item instead of discarding the proposal. A line telling the model that incident text is data, not instructions, was added before the qwen2.5:7b run. The prompt and checks were then frozen; mismatches are reported, not tuned away.
- **2026-10-22: scheduler category list.** The LLM scheduler agent's category list first omitted `application.crash` and `application.exit_code`, which the rule-based scheduler agent uses. The omission was noticed in the rule-based report and fixed before the runs reported here.
- **2026-10-23: fact matcher.** Re-running POC-GPU-001 with qwen2.5:7b showed a grounded proposal discarded because it cited `field: "xid_events.count"` with `where: "sim-gpu-node-0007:3"`, while the matcher accepted only a leaf name and a single record ID. The matcher now also accepts a dotted path that ends at the cited field (list indices ignored) and a compound ID whose every part identifies the record. The value must still match the data exactly. This was found on these cases, so results after it are not an independent test of it.
- **2026-10-26: site thresholds in the specialist prompt.** After the 7B rerun, EVAL-STOR-002 showed the model calling a 55-61% OST spread an imbalance; the rule-based agents apply the site profile's thresholds, which the LLM agents had not been given. The specialist prompt now states the site profile's thresholds for its domain (storage full and OST spread, GPU thermal limit), the same values the rules use. No case-specific hints were added. This was found on these cases, so results after it are not an independent test of it.
- **2026-11-10: skills.** LLM specialists can read repository skills (`skills/*/*/SKILL.md`) with `load_skill`. The first four skills restate domain knowledge the rule-based agents already encode (signal and walltime reading, capacity and imbalance against site thresholds, XID classes, link health). They were written after seeing the LLM agents miss some of these cases, so results with them are not an independent test; the report records the skills version used.
- **2026-11-11: reference labels in the number check.** The first Claude Opus 5.5 run discarded model text that cited tool results as T1/T2, because the check that every number in model text appears in the data read the digits in those labels as numbers. Labels that exist in the run (fetched T# results, incident items I#) are now skipped by that check; a label that doesn't exist, and every other number, is still checked. This was found on these cases, so results after it are not an independent test of it.

## Summary

| Metric | Result |
|---|---|
| Correct domain classification | 85% |
| Root cause accuracy, leading hypothesis (top-1) | 45% |
| Root cause accuracy, top-3 | 45% |
| Outcome matches label (supported / insufficient / none) | 62% |
| Correct refusal to conclude (cases labelled insufficient or none) | 67% |
| Evidence quality: open hypotheses backed by tool evidence | 100% |
| Evidence quality: required missing information identified | 10% |
| Unsupported claim rate (per open hypothesis) | 0% (0 claims) |
| Human override rate | not measured: no recorded human decisions were supplied |
| Diagnostic processing time (mean / median / max) | 21474 / 24456 / 28638 ms |
| Agent reasoning (triage + specialists, all cases) | llm: 31 |
| Model proposals discarded by the deterministic checks | 28 |
| Skills version (fingerprint of skills/) | `6c693a464e18` |

## Per-case results

| Case | Classification | Outcome (expected) | Leading hypothesis | Conf. | Top-1 | Tool-backed | Unsupported | Missing found | ms |
|---|---|---|---|---|---|---|---|---|---|
| EVAL-GPU-001 | gpu | **no_supported_hypothesis** (supported_hypothesis) | – | 0.00 | **no** | 0/0 | 0 | 0/0 | 25885 |
| EVAL-GPU-002 | gpu | **no_supported_hypothesis** (supported_hypothesis) | – | 0.00 | **no** | 0/0 | 0 | 0/0 | 16172 |
| EVAL-NET-001 | network | supported_hypothesis | network.link_down | 0.55 | yes | 1/1 | 0 | 0/1 | 28638 |
| EVAL-SCHED-001 | scheduler | supported_hypothesis | scheduler.signal_termination | 0.20 | **no** | 1/1 | 0 | 0/0 | 14303 |
| EVAL-SCHED-002 | scheduler | **no_supported_hypothesis** (supported_hypothesis) | – | 0.00 | **no** | 0/0 | 0 | 0/0 | 19638 |
| EVAL-STOR-001 | storage | supported_hypothesis | storage.capacity | 0.55 | yes | 1/1 | 0 | 0/1 | 26769 |
| EVAL-STOR-002 | **multi-domain** | no_supported_hypothesis | – | 0.00 | – | 0/0 | 0 | 0/0 | 26262 |
| EVAL-STOR-003 | storage | supported_hypothesis | storage.ost_imbalance | 0.35 | yes | 1/1 | 0 | 0/0 | 27389 |
| EVAL-TIME-001 | scheduler | supported_hypothesis | scheduler.signal_termination | 0.45 | yes | 1/1 | 0 | 0/0 | 21595 |
| EVAL-UNK-001 | unknown | no_supported_hypothesis | – | 0.00 | – | 0/0 | 0 | 1/1 | 2926 |
| POC-GPU-001 | gpu | **no_supported_hypothesis** (supported_hypothesis) | – | 0.00 | **no** | 0/0 | 0 | 0/1 | 28038 |
| POC-SCHEDULER-001 | scheduler | supported_hypothesis | node.unhealthy | 0.45 | yes | 1/1 | 0 | 0/2 | 17092 |
| POC-STORAGE-001 | **multi-domain** | **no_supported_hypothesis** (insufficient_evidence) | – | 0.00 | **no** | 0/0 | 0 | 0/4 | 24456 |

## Scenario comparison (POC scenarios)

| | POC-GPU-001 | POC-SCHEDULER-001 | POC-STORAGE-001 |
|---|---|---|---|
| Classification | gpu | scheduler | multi-domain |
| Outcome | no supported hypothesis | supported hypothesis | no supported hypothesis |
| Leading hypothesis | none | A problem on allocated node sim-node-0042, which went to DRAIN shortly before the job ended, may have led to the job being killed. The drain reason is unknown. | none |
| Leading confidence | 0.00 | 0.45 | 0.00 |
| Open hypotheses (tool-backed) | 0 (0) | 1 (1) | 0 (0) |
| Facts: reported / tool | 3 / 0 | 5 / 6 | 2 / 0 |
| Knowledge references | 0 | 4 | 0 |
| Proposed actions | 0 | 1 | 0 |
| Root cause confirmed | no | no | no |

## Unsupported claims

None detected.

## Mismatches against labels

- EVAL-GPU-001: classification=gpu, outcome=no_supported_hypothesis (expected supported_hypothesis), leading=None
- EVAL-GPU-002: classification=gpu, outcome=no_supported_hypothesis (expected supported_hypothesis), leading=None
- EVAL-SCHED-001: classification=scheduler, outcome=supported_hypothesis (expected supported_hypothesis), leading=scheduler.signal_termination
- EVAL-SCHED-002: classification=scheduler, outcome=no_supported_hypothesis (expected supported_hypothesis), leading=None
- EVAL-STOR-002: classification=multi-domain, outcome=no_supported_hypothesis (expected no_supported_hypothesis), leading=None
- POC-GPU-001: classification=gpu, outcome=no_supported_hypothesis (expected supported_hypothesis), leading=None
- POC-STORAGE-001: classification=multi-domain, outcome=no_supported_hypothesis (expected insufficient_evidence), leading=None

## Method

- Cases live in `evaluation/cases/`. Each case runs through the full workflow (triage,
  specialists, RCA Coordinator, runtime guard) in an isolated temporary data root.
- **Outcome** is `supported_hypothesis` when the leading open hypothesis has tool evidence,
  `insufficient_evidence` when it rests only on reported symptoms, and
  `no_supported_hypothesis` when nothing is open.
- **Unsupported claims** are confirmed causes, open hypotheses with confidence of at least
  0.5 and no tool evidence, and hypotheses in a case's forbidden categories.
- **Human override rate** counts REJECTED and REVISION_REQUESTED among recorded human
  decisions (`--approvals-log`). It is never simulated.
- **Next step toward a real evaluation:** representative, de-identified incidents labelled
  independently by operations engineers, plus override rates from real reviews.
