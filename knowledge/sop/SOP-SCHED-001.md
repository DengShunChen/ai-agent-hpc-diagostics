# Investigating abnormal job termination
Tags: scheduler, slurm, pjm, job failure, exit code, signal, sigkill, drain, walltime, oom

1. Record the job state, exit code, elapsed time, walltime limit and allocated nodes.
2. Exit codes above 128 mean the job was terminated by a signal (exit code minus 128). SIGKILL has several possible sources: out-of-memory kill, scheduler or administrator kill, node failure.
3. If an allocated node changed to DRAIN or DOWN, obtain the drain reason before drawing conclusions.
4. Check node memory and out-of-memory events for the failure window.
5. A failed job does not by itself indicate a scheduler fault. Correlate with storage, network and GPU evidence for the same nodes and time window.
6. Do not resume, reboot or drain nodes without an approved change.
