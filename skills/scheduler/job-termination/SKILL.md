---
name: job-termination
description: "Reading a failed job: exit codes and signals, walltime, and node state."
agents: [scheduler-agent]
---
# Why did a batch job end early?

Work from the saved job and node data. Every conclusion must cite a value you fetched.

## Exit code
- `0`: the job itself reported success; look elsewhere.
- `1`-`128`: the application exited with that code. Suspect the application first
  (category `application.exit_code`); its own output should be checked.
- Above `128`: the job was ended by a signal. Signal = exit code - 128.
  - 9 (exit 137, SIGKILL): killed. Possible causes: out-of-memory kill, a scheduler or
    administrator kill, or a node failure. Category `scheduler.signal_termination`.
  - 15 (exit 143, SIGTERM): asked to stop. Possible causes: cancellation, preemption, or a limit
    the scheduler enforced (walltime is one). Category `scheduler.signal_termination`.
  - 6 SIGABRT, 7 SIGBUS, 11 SIGSEGV (exit 134, 135, 139): an application or library fault is more
    likely than a scheduler fault. Category `application.crash`.
- An exit code alone never tells you *who* sent the signal. Say what else would be needed.

## Walltime
- Compare `elapsed` with `requested_walltime` (both `[D-]HH:MM:SS`).
- If elapsed is equal to or greater than requested, the job reached its walltime limit: a
  `scheduler.walltime` hypothesis, supported by both values.
- If elapsed is below requested, the walltime limit was not reached: you may rule
  `scheduler.walltime` out, citing both values.
- If either value is missing, list it as missing information.

## Node state
- States such as DRAIN, DRAINED, DOWN, FAIL or NOT_RESPONDING on a node the job ran on make
  `node.unhealthy` a hypothesis. Cite the node's state, and its `state_changed_at` if present.
- A drain reason that is empty is missing information, not evidence of anything.

## Do not
- Do not call a cause confirmed. Do not invent a drain reason, a memory figure or a signal sender.
