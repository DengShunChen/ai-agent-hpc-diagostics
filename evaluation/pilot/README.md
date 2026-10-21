# Pilot measurement data

Export one row per incident from the ticketing system into a CSV with the header in
`pilot-template.csv`:

| Column | Meaning |
|---|---|
| `incident_id` | ticket ID (de-identified) |
| `site` | site ID matching `config/sites/<site-id>.yaml` |
| `category` | storage, scheduler, network, gpu or cross-domain |
| `period` | `baseline` (before the platform) or `assisted` (with it) |
| `detected_at` | when the incident was detected (ISO 8601 with timezone) |
| `localized_at` | when the root cause was localized (the primary outcome) |
| `resolved_at` | when service was restored (optional; gives MTTR) |

Then run:

```bash
PYTHONPATH=src .venv/bin/python -m hpc_agent.main compare-pilot evaluation/pilot/<file>.csv
```

No results are claimed with fewer than 10 incidents per period in a category. Real pilot
files contain operational data: keep them out of Git (`evaluation/pilot/*.csv` other than
the template is git-ignored).
