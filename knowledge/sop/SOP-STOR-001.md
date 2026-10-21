# Investigating job write failures on FEFS/Lustre
Tags: storage, lustre, fefs, write failure, capacity, ost, enospc, quota

1. Confirm the failing job, its output path and the exact client-side error (for example ENOSPC or EIO).
2. Record filesystem-level utilization and per-OST utilization at the time of the failure.
3. A full filesystem is not the only cause: a single full OST can fail writes striped to it while the filesystem still has free space.
4. Check user and project quota before assuming a capacity problem.
5. Check OSS/MDS health and target states for the incident window before attributing the failure to capacity.
6. Do not free space, deactivate OSTs or migrate data without an approved change.
