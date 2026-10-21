"""Triage Agent: classify the incident domain and select specialist agents.

Works from the incident alone (no tools). Classification is a routing decision based on
which reported items mention which domain; it says nothing about the root cause.
"""

from __future__ import annotations

from hpc_agent.agents.evidence import incident_evidence, match_domains
from hpc_agent.models import Domain, Incident, TriageResult

AGENT_FOR_DOMAIN = {
    Domain.STORAGE: "storage-agent",
    Domain.SCHEDULER: "scheduler-agent",
    Domain.NETWORK: "network-agent",
    Domain.GPU: "gpu-agent",
}


class TriageAgent:
    name = "triage-agent"

    def triage(self, incident: Incident) -> TriageResult:
        evidence = incident_evidence(incident)
        signals: dict[Domain, list[str]] = {}
        for item in evidence:
            for domain in sorted(match_domains(item.statement)):
                signals.setdefault(domain, []).append(item.source)

        # Most-mentioned domain first; ties broken by name for determinism.
        ranked = sorted(signals, key=lambda d: (-len(signals[d]), d.value))
        if not ranked:
            classification = Domain.UNKNOWN
        elif len(ranked) > 1 and len(signals[ranked[0]]) == len(signals[ranked[1]]):
            classification = Domain.MULTI_DOMAIN
        else:
            classification = ranked[0]

        return TriageResult(
            incident_id=incident.incident_id,
            classification=classification,
            related_domains=[d for d in ranked if d != classification],
            domain_signals={d: signals[d] for d in ranked},
            selected_agents=[AGENT_FOR_DOMAIN[d] for d in ranked],
            symptoms=list(incident.symptoms),
            available_evidence=evidence,
            missing_evidence=self._missing(incident, signals),
        )

    @staticmethod
    def _missing(incident: Incident, signals: dict[Domain, list[str]]) -> list[str]:
        missing = []
        if not signals:
            missing.append("symptoms specific enough to identify an affected domain")
        if incident.timestamp is None:
            missing.append("incident timestamp")
        if incident.node is None:
            missing.append("affected node name")
        if incident.job_id is None and Domain.SCHEDULER in signals:
            missing.append("job ID")
        if not incident.alerts:
            missing.append("monitoring alerts for the incident window")
        if not incident.logs:
            missing.append("relevant log excerpts")
        return missing
