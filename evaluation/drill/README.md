# Synthetic drill: does the agent shorten root-cause analysis?

A tabletop exercise on simulated incidents with known, injected root causes. Engineers analyse
each scenario either **with raw logs only** (`baseline`) or **with the agent's report as well**
(`assisted`). The time each takes to name the root cause, and whether they named the right one,
are compared with the existing `compare-pilot` analysis.

**What a drill can show:** the difference *on this simulator's scenarios*. **What it cannot show:**
MTTR on the real cluster. The scenarios are synthetic, the fault catalog was written by the POC
developer, and log volume is far smaller than on a real system. Report drill results as such and
never as an MTTR reduction (see docs/business-impact.md).

## Why a drill is needed

On the target cluster (3840 FX1000 nodes and 24 RX2570 M6 + A100 nodes), hard incidents arrive about
1-3 times a month and take 1-3 days each to analyse. The pilot protocol asks for at least 10
incidents per period per category, which at that rate takes many months. A drill gives an early,
controlled measurement; replaying past tickets (de-identified, through `import-export`) is the
step after it, and the live pilot remains the only evidence about MTTR.

## Decided before the first drill set was generated (2026-11-12)

- The assisted arm's report comes from `drill-prepare --reasoning cascade` with the local qwen2.5:7b: the rules
  first (with the log digest), the LLM agents' second opinion only where the rules lead with an outcome. The drill
  measures the system the award entry describes. Not changed after seeing any drill data.
- Scored as: median minutes per arm and share of correct answers per arm, reported together.

## Decided before any drill answer was collected (2026-11-13)

- Three engineers take part: E1, E2, E3 (sessions on 2026-11-18 and 2026-11-19). E4 is worked by the POC developer,
  who co-wrote the fault catalog and knows how the agent's report reads. E4's answers are reported on a row of
  their own ("developer self-test"), never pooled with E1–E3 and never used for a claim. The main result is
  `drill-score` on the sheets of E1, E2 and E3 only; E4's sheet is scored on its own against the same labels.
- The developer works E4 only after the three engineer sessions (from 2026-11-20), so the person running the
  sessions has not worked through the scenarios.
- With three engineers the groups are uneven (group 1: E1, E3; group 2: E2). Each arm still gets 18 answers
  (3 × 12 / 2), but half A's scenarios are answered without the agent twice as often as with it, and half B's the
  other way round, so a difference in difficulty between the halves would show up as a difference between the arms.
  This is reported with the result.

## Steps (facilitator)

The facilitator is **not** the developer of the agents: the developer must not see the blind set's labels or
logs before the drill (Codex or the drill owner runs steps 1, 2 and 5).

1. **Generate a blind set** outside the repository. Keep the seed; don't look at `labels/`:

   ```bash
   PYTHONPATH=src .venv/bin/python -m hpc_agent.main simulate --blind-seed <random number> --count 12 --faults all --platform tcs-fx1000 --output ~/drill-2026-11/sim
   ```

   `--count` draws from one platform (`--platform both` needs `--per-fault`); to mix platforms, generate each
   half with the same API and merge them, as the evaluator did for the first drill set.

2. **Write the packets** (imports the export, diagnoses every scenario, splits halves A/B and the engineers
   into two groups, crossover; never reads or copies `labels/`):

   ```bash
   PYTHONPATH=src .venv/bin/python -m hpc_agent.main drill-prepare ~/drill-2026-11/sim --output ~/drill-2026-11/out --engineers E1,E2,E3,E4
   ```

   | | Half A | Half B |
   |---|---|---|
   | Group 1 (E1, E3) | baseline (raw files only) | assisted (raw files + agent report) |
   | Group 2 (E2, E4) | assisted | baseline |

   Each engineer gets `out/packets/<code>/`: one folder per scenario with `raw/` (logs, command output,
   incident summary) and, when assisted, `agent-report.txt` and `names.txt` (pseudonym → simulated name; log
   line numbers are the same in the report and the raw files), plus `answer-choices.txt` and `sheet.csv`.
   The assisted report comes from the rule-based agents by default (`--reasoning cascade` adds the LLM
   agents' second opinion; needs a model). It lists the warning and error lines on the job's nodes while it
   ran (`get_log_digest`), which is where an engineer would start grepping.

3. **Run the drill.** Each engineer works through the folders in order. For each scenario they write in
   `sheet.csv`: `started_at` when they open the folder, `answered_at` when they state the root cause (ISO
   8601 with a timezone, for example `2026-11-20T09:00:00+08:00`), `answer_category` from
   `answer-choices.txt`, a short `answer_text`, and `confidence`. Suggested time cap: 30 minutes per
   scenario (record the cap time and `insufficient_evidence` if reached).

4. **Collect** the sheets (engineer codes only, never names).

5. **Score and compare**:

   ```bash
   PYTHONPATH=src .venv/bin/python -m hpc_agent.main drill-score ~/drill-2026-11/out/packets/*/sheet.csv --labels ~/drill-2026-11/sim/labels --output ~/drill-2026-11/score
   PYTHONPATH=src .venv/bin/python -m hpc_agent.main compare-pilot ~/drill-2026-11/score/drill-results.csv --min-n 10
   ```

   `drill-summary.md` gives median minutes and the share of correct answers per arm, side by side: a faster
   wrong answer is not an improvement. With 4 engineers × 12 scenarios there are 24 answers per arm
   (18 in the 2026-11 drill, which has three engineers in the main comparison; see above).

Engineers get `engineer-guide.zh-TW.md` (instructions in Traditional Chinese).

Keep filled-in drill files out of Git, like pilot files. Engineer codes, not names.
