# Simulated Scenarios: Evaluation

Generated 2026-11-11 with `hpc-agent evaluate --scenarios` on scenarios written by `hpc-agent simulate`
(`src/hpc_agent/simulate/`). Each scenario is a simulated export (raw scheduler logs, syslog, command
output) with one injected root cause, background noise and misleading signals. It goes through the same
path as a real site export: `import-export --adapter raw` (contract checks, pseudonymization, credential
scan) into a temporary data root, then the full workflow. Labels are kept in a separate directory the
import adapter never reads.

> **Read this first.** The scenarios are synthetic and the fault catalog, the log formats and the labels
> were all written by the POC developer. The numbers show how the agents behave **on this simulator**.
> They are not evidence of accuracy on real incidents, and **no MTTR or analysis-time reduction is
> claimed** (see [business-impact.md](business-impact.md)).

## Scenario sets

| Set | Platform | Scenarios | Faults covered | Log lines per scenario |
|---|---|---|---|---|
| `evaluation/scenarios/dev/` | `slurm-x86`: Slurm (slurmctld, slurmd), InfiniBand, Lustre | 14 (seeds 1001-1014) | all 12 | about 1,900-3,100 |
| `evaluation/scenarios/fx1000/` | `tcs-fx1000`: PJM, Tofu, FEFS, FX1000 + RX2570 M6/A100 | 14 (seeds 2001-2014) | all 12 | about 1,900-3,800 |
| `evaluation/scenarios/novel/` | both | 10 (seeds 3001-3010) | 5 novel faults, not in the rules | about 1,400-2,000 |

Both sets are **development sets**: they were looked at while the simulator and the log tool were built.
A blind set (`simulate --blind-seed`) has not been evaluated yet.

## Results

| Metric | Rules, Slurm set | qwen2.5:7b, Slurm set, first run | qwen2.5:7b, Slurm set, after the import fix | Rules, FX1000 set |
|---|---|---|---|---|
| Correct domain classification | 86% | 43% | 43% | 86% |
| Root-cause category, top-1 | 77% (10/13) | 31% (4/13) | 46% (6/13) | 77% |
| Root-cause category, top-3 | 77% | 38% | 46% | 85% |
| Outcome matches label | 93% | 86% | 93% | 100% |
| Correct refusal to conclude | 100% (1/1) | 0% (0/1) | 0% (0/1) | 100% |
| Open hypotheses backed by tool evidence | 100% | 86% | 92% | 100% |
| Unsupported claim rate | 0% | 0% | 4% (1 claim) | 0% |
| Scenarios with an injected key log line cited | 0 of 13 (rules never search logs) | 0 of 13 | **2 of 13** | 0 of 12 (rules never search logs) |
| Mean time per scenario | 8 ms | 330 s | 335 s | 9 ms |
| Model proposals discarded by the checks | – | 19 | 20 | – |

"After the import fix" ran on commit 096f6ce (job-manager log lines no longer carry a pseudonymized node,
and the evaluator counts cited scheduler-log key lines). Both cited key lines were in the scheduler log
(SIM-DEV-003 walltime, SIM-DEV-004 administrator cancel), which the first run's import made unusable and
its scoring could not count, so the change mixes the import fix, the counting fix and run-to-run variation
(decoding is deterministic, but tool results and prompts changed). The model still misses all three GPU
hardware faults, both cascades and the storage capacity faults. Top-1 and top-3 count the root-cause
**category**, not the mechanism. The 7B run used Ollama (`qwen2.5:7b`, digest 845dbda0ea48, Q4_K_M) on an
8-core CPU with no GPU, temperature 0, seed 42, `HPC_AGENT_LLM_TIMEOUT=900`. The 7B model was not run on
the FX1000 set, and Claude was not run on either set.

### Where the rules miss

- **Network cascades** (`ib_flap_lustre`, SIM-DEV-009/014, SIM-FX-009/014): link flaps → LNet timeouts →
  job I/O failure. Triage routes on the user's symptom ("job failed"), so the network agent is not
  consulted and the scheduler agent reports a non-zero exit code. The rules never read logs, and the link
  counters alone are not routed to.
- **OOM on Slurm** (SIM-DEV-001): the simulated `sacct` records `OUT_OF_MEMORY` with exit code `0:125`,
  which the adapter does not turn into an exit code (it only maps signals 1-64), so no evidence reaches
  the scheduler agent. On FX1000 the `pjstat` reason is parsed, and the same fault is found.
- **Memory ECC node failure on FX1000** (SIM-FX-005): the rules lead with `scheduler.signal_termination`
  (the job was killed with signal 9) instead of `node.unhealthy`.

### Where qwen2.5:7b misses

- **GPU faults** (SIM-DEV-006/007/008): the model led with `application.crash` or a storage error instead
  of the GPU hardware fault that `dcgmi` and the XID events show. Triage routed mostly to the scheduler.
- **Storage and cascades**: `storage.io_error` with low confidence in place of `storage.capacity`, and no
  supported hypothesis for the first cascade.
- **Refusal**: on `logs_missing` (SIM-DEV-012) it reported `insufficient_evidence` with a
  `scheduler.signal_termination` guess instead of no supported hypothesis.
- **Logs**: it searched 2-5 times per scenario but never produced a log fact the checks kept.

**One traced run (SIM-DEV-006, `gpu_xid79`, rerun with `diagnose` to read the trace).** Triage routed to
GPU and scheduler. Both agents searched only `slurmctld` for the job ID; neither searched `syslog`,
where the injected NVRM XID 79 lines are. The GPU agent did read `gpu_health` (GPU 1: `failure`, XID 79),
but called it an application-class XID, and all three of its proposals were discarded because every
fact named node `node-6e54…` instead of `node-c8d3…`. `node-6e54…` is the pseudonym of `slurmctld`,
the node field of every scheduler-log line: the importer pseudonymizes it like a host name, so the
controller looks like a compute node, and the model took it for the job's node. The scheduler agent's
`application.crash` (exit code 1) was the only proposal kept. The checks did their job (no wrong fact
reached the report), but the model's answer was wrong for three separate reasons: search strategy,
XID knowledge, and a misleading node name in the imported scheduler log. The last one is an import
issue, fixed after this run (see the disclosure below).

## Novel faults: rules baseline (frozen 2026-11-11, before Phases 2-4)

`evaluation/scenarios/novel/` holds 10 scenarios (5 faults × 2 platforms, seeds 3001-3010) with faults that
**were chosen because the rule-based agents do not encode them**. They test whether an agent can work out a
cause nobody wrote a rule for. They are not a fair accuracy comparison: the rules lose here by design,
and that is what the set is meant to show. Each label's leading category is in the matching LLM agent's
category list, so an LLM agent *can* score; labels forbid only answers that are actually wrong (a slow
node's job really did hit its time limit, so `scheduler.walltime` is not forbidden there).

| Fault | Where the evidence is | Leading label | Rules lead with |
|---|---|---|---|
| `slow_node_walltime`: one node thermally throttled; the coupled job hits its time limit | logs | `node.unhealthy` | `scheduler.walltime` (both platforms) |
| `link_degraded_mpi`: link stays up but corrupts packets; MPI aborts (no link-down) | logs + command output (`ibstat` / `tofu_links` error counters) | `network.packet_errors` | `application.exit_code`; network agent never consulted |
| `mds_slow_hang`: metadata server stops answering; job hangs until its time limit | logs | `storage.health` or `storage.io_error` | `scheduler.walltime` |
| `gpu_pcie_aer`: PCIe AER errors on one GPU's link; NCCL fails | logs + command output (`dcgmi` health `Warning`) | `gpu.interconnect` | `application.exit_code`; GPU agent ran but did not connect the warning |
| `clock_skew_auth`: node lost time sync; credentials look expired and launches fail | logs | `node.unhealthy` | `application.exit_code` (a forbidden answer: 1 unsupported claim each) |

**Rules: top-1 0%, top-3 0%, classification 60%, 2 unsupported claims** (10 scenarios, 8 ms each).

- Misses on **logs-only** faults (6 of 10) are a design limit, not a reasoning gap: the rules never read logs.
- Misses on the **logs + command output** faults (4 of 10) are the interesting ones: the evidence was in
  data the rules read, but no rule connects it to the cause, or triage never routes to the agent that
  would see it.
- The background noise contains routine lines from the same daemons (`chronyd` "clock wrong by 0.000412
  seconds", harmless Lustre reconnects, an old recovered link flap), so a keyword hit is not an answer.
- These scenarios will be looked at while building Phases 2-4, so later results on them are **not** an
  independent test. A blind set drawn after the version is frozen (`simulate --blind-seed N --faults novel`)
  tests **unseen instances from the same simulator and fault catalog**; it is not an independently designed
  fault set. Independently designed cases (separate causal chains, messages and distractors, not shown to
  the implementer before the run) are the third tier; see [evaluation-protocol.md](evaluation-protocol.md).

## What this shows

1. **The simulator and the import path work end to end.** Raw logs and command output go through the
   same adapter, contract checks and pseudonymization a real export would, and the agents run on the
   result. This is the main deliverable of this step.
2. **The scenarios are harder than the 13 hand-written cases**, as intended. The rules drop from 100% to
   77% top-1, and the misses are the ones a real operator would care about: cascades across domains.
3. **The local 7B model does not use the logs usefully.** It calls `get_log_lines`, but no scenario ended
   with an injected key line as evidence, and its root-cause accuracy is below the rules. On this
   hardware it also takes about 5.5 minutes per scenario.
4. **Nothing here measures analysis time.** That needs the human drill (`evaluation/drill/`): engineers
   analysing the same scenarios with and without the agent's report, with time *and* correctness
   recorded per arm.

## Limitations

- **Synthetic, developer-written.** The fault catalog, the timing between symptoms, the distractors and
  the labels are one person's model of these faults. An agent that matches this model may still fail on
  real incidents.
- **TCS formats are approximations.** PJM, `pjstat`, Tofu and FEFS messages were written from general
  knowledge of those products, not copied from real output. Before a drill on the target cluster they
  should be checked against a few de-identified real lines.
- **Small log volume.** About 2,000-4,000 lines per scenario, limited by the 1 MB per-file limit of the
  data adapter and the 6,000-line log index. A real day on 3,840 nodes is orders of magnitude larger.
- **Limited command coverage.** `dcgmi` health is exported for the job's GPU node only; host names that
  appear only inside free log text are not pseudonymized (all names are `sim-` here).
- **Rule-based agents draw no hypothesis from logs.** Until 2026-11-12 they did not read logs at all, so the
  log-evidence column of earlier runs only measures the LLM agents. Since then the scheduler agent quotes the
  log digest (see Disclosure); a quoted key line counts as cited, but the rules' hypotheses do not change.
- **Small samples.** 13-14 scenarios per set; one scenario is 7-8 percentage points.

## Disclosure: changes made during this work

- **Labels:** storage faults (`ost_full`, `ost_imbalance`) accept `storage` or `multi-domain` as the
  classification. This was changed after the first rules run on the dev set, where the rules classified
  them as `multi-domain` with the storage agent's tool evidence leading.
- **Generator:** the OST usage baseline was narrowed from 41-68% to 50-62% before any LLM run, so that
  scenarios without an imbalance fault stay under the site's OST-spread threshold.
- **Agent definitions:** `get_log_lines` was added to the scheduler, GPU, network and storage agents, with
  one prompt paragraph on how to cite a log line (the exact `text`, `where` = line number), and their
  limits were raised to `max_rounds` 5 and `max_tool_calls` 8 so a log search does not use up the budget.
  No scenario-specific hints were added.
- **Number check (2026-11-11):** T#/I# reference labels are skipped by the check that every number in
  model text appears in the data (see [evaluation-llm.md](evaluation-llm.md)). Made on the 13 cases, before
  these runs.
- **Job manager log node (2026-11-11, after the 7B run):** the traced SIM-DEV-006 run showed that the
  importer gave every `slurmctld`/`pjm` line the node `slurmctld`/`pjm`, which was then pseudonymized
  like a host (`node-6e54…`) and looked like the job's compute node. Those lines now carry no node; the
  daemon name is no longer pseudonymized or replaced in log text. The same look found that the
  evaluator could never count a cited scheduler-log key line (labels name the controller host, the
  index named the daemon); it now matches those lines by source and line number. Rules results on both
  sets are unchanged (rules don't read logs). The 7B dev set was rerun after it (see Results).
- **Phases 2-4 prompt and limits (2026-11-11, after the novel-fault rules baseline was frozen):** the four
  specialists may call `analyze_logs` and propose a `consult`, their prompt asks them to form a hypothesis,
  test it and revise it, and to compare counts, nodes and timing before trusting a keyword hit; limits rose
  to `max_rounds` 8 and `max_tool_calls` 12. The text names no fault from any scenario set. All LLM results
  above were measured before this change.
- **Evaluation code (2026-11-11, from the independent evaluator's review):** failed cases are kept with
  outcome "failed"; labels gained `platform` and `components` (exports unchanged); reports add the faulty
  component check, counts by group and platform, and model completion apart from rules fallback.
- **Consulted-agent fallback (2026-11-11, found by the independent evaluator's audit of the paired Claude run on
  `eval-freeze-1`):** when an agent started by a consult could not use the model and fell back to the rules, its
  diagnosis lost `consulted_by`, so the workflow left it out of `run.diagnoses` (the RCA still used it) and the
  evaluation counted the run as model-complete. On the 8 paired cases this reported model completion 4/8 where
  the traces show 3/8. The fallback now keeps `consulted_by`, the run keeps every agent that ran, and model completion is
  cross-checked against the trace (a fallback in the trace that the run record lacks counts as incomplete). Diagnoses
  are unchanged; only the run record and the completion count change. Results measured on `eval-freeze-1` are
  not rerun and carry this defect.
- **Cascade mode (2026-11-12, after the phase-1 comparison on `eval-freeze-1`):** `--reasoning cascade` runs the
  rules first and hands a case to the LLM agents only when the rules lead with an outcome category
  (`scheduler.walltime`, `scheduler.signal_termination`, `application.exit_code`, `gpu.unexplained`: how a job
  ended, not which component caused it). The idea came from what the dev and novel-dev sets showed: on the
  novel faults the rules lead with exactly these categories. The gate and the final-answer rule were written
  before any cascade run and were not tuned on scores; the phase-1 evaluation results were not used to shape
  them. Known limit: a rules miss with no tool-supported hypothesis (SIM-DEV-001) is treated as a refusal and
  is not handed over. On the dev sets the gate hands over 20 of 38 cases, including 5 the rules already get right.
- **Budget crash in knowledge lookups (2026-11-12, found in the first cascade dev run with qwen2.5:7b):**
  SIM-NOV-009 failed with `ToolNotAllowedError: tool call budget exhausted (gpu-agent -> search_sop)`: the LLM
  agent had spent the run's shared call budget, and the knowledge lookup after its answer raised instead of
  stopping. Knowledge references are optional context (never evidence), so a spent budget now skips them and
  records "knowledge lookup skipped" in the checks; no call is made past the budget, and evidence tools still
  fail closed. The same defect could hit any LLM run on `eval-freeze-1`; those results are not rerun.
- **First cascade dev run (qwen2.5:7b, before the fix above):** 0 cases rescued, 3 cases the rules had right
  turned wrong (SIM-DEV-004, SIM-FX-001, SIM-FX-003: a checked but wrong LLM cause led over a correct rules
  answer). **Changed after this run (2026-11-12):** the rules' lead now stays first and the LLM agents' checked
  cause is listed right below it as a second opinion; the gate is unchanged. Cascade top-1 therefore equals the
  rules' top-1 by construction, and what the cascade can add is measured as "rules wrong, second opinion right"
  (reported per run). These were the claim metrics fixed before any independent test: right-to-wrong flips = 0,
  second opinion right where the rules are wrong, and key log lines quoted.
- **Log digest for the rules (2026-11-12, after the first cascade dev run):** the rules report listed "relevant
  log excerpts" as missing and pointed at no log line, so an engineer given the report still had to grep from
  scratch; qwen2.5:7b cited a key line in 2 of 13 Slurm dev scenarios when it ran every agent (Results table), 4
  of 31 on the phase-1 hidden set, and 2 of the 19 dev cases the first cascade run handed to it. `get_log_digest` is
  a fixed tool (no model, no arguments): warning and error lines on the failed job's allocated nodes from job start
  to 5 minutes after its end, matched by a fixed list of generic error terms, repeats folded, oldest first; when
  there are too many, the earliest and the latest are kept and the middle is omitted (counted); lines on other
  nodes in the window are only counted. The rule-based scheduler agent quotes the lines as facts and draws no hypothesis
  from them. The term list and window were written before measuring, but by the developer who also wrote the fault
  catalog, so the dev result below is not independent. Dev check (rules): top-1 and classification unchanged on
  all 38 dev cases; a key line was quoted in **32 of 35** dev scenarios that have key lines (dev 11/13, FX1000
  11/12, novel 10/10; misses: SIM-DEV-003 walltime, SIM-DEV-004 and SIM-FX-004 admin cancel, whose key lines carry
  no error term). Digests hold 0-6 lines on these ~2,000-line logs; real logs are far
  larger, and digest size there is unmeasured.
- Results on the dev sets after these changes are not an independent test of them. A blind set from the
  same catalog tests unseen instances only; independently designed cases are the stronger test
  ([evaluation-protocol.md](evaluation-protocol.md)).

## How to reproduce

```bash
PYTHONPATH=src .venv/bin/python -m hpc_agent.main simulate --scenarios evaluation/scenarios/dev --output ~/sim-dev
PYTHONPATH=src .venv/bin/python -m hpc_agent.main evaluate --scenarios ~/sim-dev
MODEL_PROVIDER=ollama MODEL_NAME=qwen2.5:7b HPC_AGENT_LLM_TIMEOUT=900 \
  PYTHONPATH=src .venv/bin/python -m hpc_agent.main evaluate --scenarios ~/sim-dev --reasoning llm
```

The output directory must be outside the repository and new or empty.

## Next steps

1. Formal evaluation under [evaluation-protocol.md](evaluation-protocol.md), run by the independent evaluator.
2. Calibrate the TCS formats against a few de-identified real PJM, Tofu and FEFS lines.
3. Evaluate a blind set once, with the seed kept by someone other than the developer.
4. Run the human drill (`evaluation/drill/README.md`) to measure time and correctness with and without
   the agent's report.
5. Replay past tickets from the target cluster through `import-export --adapter raw`.
