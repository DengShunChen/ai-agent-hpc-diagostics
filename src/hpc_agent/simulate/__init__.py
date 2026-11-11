"""Offline simulated cluster: raw logs and command outputs with an injected root cause.

Nothing here connects to a host or runs a command. Every name is a `sim-` name, every file is
marked synthetic, and the formats only imitate the public shape of Slurm, syslog, Lustre,
InfiniBand and DCGM output; they are not produced by those tools.

    scenario spec (YAML) --generate()--> <out>/export/<id>/...   (what a site would export)
                                         <out>/labels/<id>.json  (the answer, kept apart)

The export is read by hpc_agent.ingest.raw_export and imported like any site export.
"""
