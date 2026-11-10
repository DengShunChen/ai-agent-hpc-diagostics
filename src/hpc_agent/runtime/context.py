"""What the harness tells a model about the incident. Incident text is labelled as items (I1, I2, ...)."""

from __future__ import annotations

from hpc_agent.agents.evidence import incident_evidence
from hpc_agent.models import Evidence, Incident


def incident_items(incident: Incident, with_source: bool) -> tuple[list[Evidence], str]:
    reported = incident_evidence(incident)
    if with_source:
        lines = [f"I{i}: [{e.source.split(':', 1)[1]}] {e.statement}" for i, e in enumerate(reported, 1)]
    else:
        lines = [f"I{i}: {e.statement}" for i, e in enumerate(reported, 1)]
    return reported, "\n".join(lines)


def incident_brief(incident: Incident, items: str) -> str:
    when = incident.timestamp.isoformat() if incident.timestamp else "unknown"
    return (f"Incident {incident.incident_id} (severity {incident.severity}, node {incident.node or 'unknown'}, "
            f"job {incident.job_id or 'unknown'}, time {when})\n{items or 'no reported items'}")
