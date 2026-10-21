# Write failures caused by a single full OST
Tags: storage, write failure, ost, imbalance, enospc, capacity

Synthetic past incident. Jobs failed writing checkpoints with ENOSPC while aggregate
filesystem utilization was 81%. Per-OST data showed one OST at 99%. Resolution: data on the
full OST was rebalanced under an approved change. Lesson: check per-OST utilization, not
only the filesystem total.
