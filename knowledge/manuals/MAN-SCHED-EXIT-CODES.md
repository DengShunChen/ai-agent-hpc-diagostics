# Job exit codes and signals
Tags: scheduler, exit code, signal, sigkill, sigterm, sigsegv, job

A job exit code above 128 indicates termination by signal number (code - 128).
137 = SIGKILL (9), 143 = SIGTERM (15), 139 = SIGSEGV (11), 134 = SIGABRT (6).
The signal shows how the process ended, not why.
