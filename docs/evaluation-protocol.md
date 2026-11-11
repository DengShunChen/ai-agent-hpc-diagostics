# Evaluation Protocol: Rules, Local 7B and Claude

How the three reasoning modes are compared. Draft for review by the independent evaluator (Codex);
it becomes binding when the evaluated version is frozen (a git tag such as `eval-freeze-1`).

**Roles.** The implementer (Claude) builds and fixes the system. The independent evaluator (Codex)
designs and holds the blind and independent cases, runs the formal evaluation and checks the results.
The user decides disputes. The evaluator writes only outside the repository or in
`docs/evaluation-independent.md`; the implementer does not read hidden cases or labels before a run.

## 1. Case tiers (always reported separately)

| Tier | What it is | Who sees it | What it can show |
|---|---|---|---|
| Development | `evaluation/cases/`, `evaluation/scenarios/{dev,fx1000,novel}` | everyone; used for debugging | regressions; not accuracy |
| Unseen instances | blind sets drawn **after** the freeze from the same catalog (`simulate --blind-seed N --per-fault K`) | evaluator only until the run | generalization to new instances of the same simulator; **not** independent fault design |
| Independent cases | causal chains, messages and distractors designed by the evaluator, outside the repository | evaluator only until the run | the strongest test available here; still synthetic |

`novel` faults were chosen because the rules don't encode them: they test capability boundaries, not
overall accuracy on typical incidents. Known gaps in all tiers: multiple simultaneous faults, healthy
(no-fault) incidents, contradictory evidence. These are listed as gaps, not claimed.

## 2. Sampling and denominators

- Blind sets are stratified: exactly K scenarios per fault and platform (`--per-fault K --platform both`),
  with known, novel and missing-data faults reported as separate groups.
- Every metric is reported as a count (numerator / denominator) as well as a rate. Re-running a model on
  the same case is not a new case.

## 3. Two comparisons

- **End to end** (implemented): each mode starts from the incident and does its own triage, searching
  and diagnosis. Measures the whole system.
- **Fixed evidence** (not implemented yet): every mode gets the same evidence package (the relevant
  structured data and log lines, without labels) to separate "did not find the evidence" from "read it
  wrongly". Where the rules cannot read a format, that is reported as a capability limit; no rule is
  added for the occasion.

## 4. Metrics

Automatic (from `hpc-agent evaluate`):

| Metric | Meaning and limits |
|---|---|
| Root-cause **category** hit (top-1, top-3) | the leading category matches the label (for example `node.unhealthy`); does **not** show the specific mechanism |
| Faulty component named | the leading hypothesis names the labelled node or OST; only for infrastructure faults (OOM, segfault, walltime, cancel and application XIDs have none) |
| Domain classification, outcome, correct refusal | as in docs/evaluation.md |
| Key log line cited | an injected key line was cited as checked evidence; does **not** show the causal chain was understood |
| Tool-backed hypotheses, unsupported claims | "has some tool evidence" is not "evidence supports this cause"; the unsupported-claim check covers specific conditions only, so 0% is not "no unsupported statement" |
| Failed cases | timeouts, exhausted budgets, import failures and other errors are kept and scored as wrong |

Human review (sampled, blinded to the mode, fixed rubric agreed before the run): mechanism correct, faulty
component, time relationship, and whether each cited piece of evidence is relevant to the cause.

## 5. What every formal run records

`hpc-agent evaluate --scenarios DIR --runs-dir RUNS --json RESULTS.json --output REPORT.md` keeps:

- `RUNS/<id>.run.json`: the full DiagnosticRun (or the error), wall time;
- `RUNS/<id>.trace.jsonl`: the agent trace (LLM modes; credential-like content withheld);
- `RUNS/manifest.json`: commit and tags, hashes of `src/`, `config/agents`, `config/sites`, `knowledge/`,
  the scenario export and labels, the skills version, model provider, name and timeout, host, start and
  finish times, failed-case count, Claude token use;
- `RESULTS.json`: per-case scores and log results (key lines, searches, analyses, consults, component
  hit, fallbacks, errors).

Still to be recorded by hand in the evaluator's report: Ollama model digest and quantization, decoding
settings (temperature 0, seed 42 in this POC), dependency lock hash.

## 6. Run rules

1. Freeze: the implementer tags the evaluated commit with a clean work tree and reports the full test
   summary; the evaluator re-runs the tests to confirm.
2. No skill is installed and no prompt, label or threshold changes during a formal batch.
3. Resource policy fixed per mode before the run: tool-call budget and rounds come from
   `config/agents/*.md`; `HPC_AGENT_LLM_TIMEOUT=900` for local models; Claude under the spend cap.
4. Claude runs need the user's approval of the amount. If the budget does not cover the full set, a
   pre-selected small paired subset is run (same cases for all three modes) and reported as such.
5. Any change made after seeing formal results is disclosed and tested on a new version with new cases.

## 7. Reporting

Per tier and group: counts and rates for every metric, failure reasons, cost and time per case. No MTTR or
analysis-time claim: that needs the human drill (`evaluation/drill/`) or a pilot (docs/business-impact.md).
