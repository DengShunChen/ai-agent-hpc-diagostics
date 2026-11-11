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

## Steps (facilitator)

1. **Generate a blind set** outside the repository. Keep the seed, and don't look at `labels/`:

   ```bash
   PYTHONPATH=src .venv/bin/python -m hpc_agent.main simulate --blind-seed <random number> --count 12 --output ~/drill-2026-11
   ```

2. **Prepare the assisted material**. Import the export with the public simulation key (simulated
   data only), copy the knowledge base, agent and site configuration and skills, and diagnose each scenario:

   ```bash
   export HPC_AGENT_PSEUDONYM_KEY=simulated-scenarios-only-not-a-secret-0000
   PYTHONPATH=src .venv/bin/python -m hpc_agent.main import-export ~/drill-2026-11/export --adapter raw --output ~/drill-2026-11/root
   cp -r knowledge config skills ~/drill-2026-11/root/
   HPC_AGENT_DATA_ROOT=~/drill-2026-11/root PYTHONPATH=src .venv/bin/python -m hpc_agent.main diagnose SIM-BLIND-001
   ```

   The agent's report uses pseudonyms (`node-1a2b…`); `names/<id>.txt` maps them to the simulated
   names in the raw files. Log line numbers are the same in the report and in the raw files.

3. **Split the scenarios into halves A and B** and the engineers into two groups (crossover):

   | | Half A | Half B |
   |---|---|---|
   | Group 1 | baseline (raw logs only) | assisted (raw logs + agent report) |
   | Group 2 | assisted | baseline |

   Each engineer sees each scenario once. Give them `export/<id>/` (and, when assisted, the
   report and `names/<id>.txt`), never `labels/`.

4. **Record** one row per engineer and scenario in a copy of `drill-template.csv`:
   `incident_id` = `<engineer code>-<scenario id>`, `site` = `drill`, `category` = the domain of the
   injected fault (filled in afterwards from `labels/`), `period` = `baseline` or `assisted`,
   `detected_at` = when they started, `localized_at` = when they stated the root cause,
   `resolved_at` left empty. Record their answer in a copy of `answers-template.csv`.

5. **Score and compare**. Compare each answer with `labels/<id>.json` (fault and leading categories),
   then:

   ```bash
   PYTHONPATH=src .venv/bin/python -m hpc_agent.main compare-pilot drill-results.csv --min-n 10
   ```

   Report median time per arm **together with** the share of correct answers per arm: a faster
   wrong answer is not an improvement.

Keep filled-in drill files out of Git, like pilot files. Engineer codes, not names.
