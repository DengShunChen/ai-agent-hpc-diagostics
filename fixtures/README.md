# Fixtures

All content is **synthetic**. Identifiers use the `sim-` / `SIM-` prefix and are not
real hosts, jobs or users. Never place production data, hostnames, IP addresses,
usernames or credentials here — the loader rejects credential-like content.

- `incidents/` — normalized `Incident` JSON, one file per incident ID.
- `../test_data/<domain>/<incident_id>/<dataset>.json` — saved tool outputs read by the Tool Gateway.
  A missing file means *no data*; tools report `no_data` rather than inventing values.

`POC-STORAGE-001` intentionally has **no** storage test data (see docs/scenarios.md).
