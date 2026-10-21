# Job SIGKILL traced to out-of-memory on one node
Tags: scheduler, sigkill, exit code 137, oom, memory, job failure

Synthetic past incident. A job ended with exit code 137 well before its walltime. The
node kernel log showed an out-of-memory kill. The scheduler itself was healthy.
Resolution: the user's memory request was corrected.
