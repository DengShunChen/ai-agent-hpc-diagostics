"""RCA Coordinator: correlate specialist findings into one RCAResult.

The coordinator ranks hypotheses and relates findings across domains. It never confirms
a root cause: in this POC, confirmation is a human decision made after reviewing the
evidence. It adds no facts of its own; every fact comes from the incident or a tool.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from hpc_agent.agents.evidence import dedupe
from hpc_agent.models import (
    AgentDiagnosis,
    Domain,
    Evidence,
    EvidenceKind,
    HypothesisStatus,
    Incident,
    RCAResult,
    TriageResult,
)

# Checks suggested for a node implicated in one domain when another domain was not examined.
_NODE_CHECKS = {
    Domain.STORAGE: "get_filesystem_usage()",
    Domain.NETWORK: "get_network_status()",
    Domain.GPU: "get_gpu_health()",
}


def _unique(items: Iterable[Evidence]) -> list[Evidence]:
    return list({(e.source, e.statement): e for e in items}.values())


class RCACoordinator:
    name = "rca-coordinator"

    def correlate(
        self,
        incident: Incident,
        triage: TriageResult,
        diagnoses: Sequence[AgentDiagnosis],
        unavailable_agents: Sequence[str] = (),
    ) -> RCAResult:
        causes = [c for d in diagnoses for c in d.possible_causes]
        # Stable sort: equal confidence keeps triage/agent order.
        open_causes = sorted(
            (c for c in causes if c.status is HypothesisStatus.HYPOTHESIS), key=lambda c: -c.confidence
        )
        ruled_out = [c for c in causes if c.status is HypothesisStatus.RULED_OUT]

        affected = dedupe([c.domain for c in open_causes if c.domain is not None])
        tool_backed = {
            c.domain for c in open_causes if any(e.kind is EvidenceKind.TOOL_RESULT for e in c.supporting_evidence)
        }
        classification = Domain.MULTI_DOMAIN if len(tool_backed) >= 2 else triage.classification

        missing = dedupe(
            [*triage.missing_evidence, *(m for d in diagnoses for m in d.missing_information)]
            + [f"{agent} not available in this iteration: its domain was not examined" for agent in unavailable_agents]
        )
        checks = dedupe(
            [*(c for d in diagnoses for c in d.recommended_checks), *self._cross_domain_checks(diagnoses)]
        )

        return RCAResult(
            incident_id=incident.incident_id,
            classification=classification,
            affected_domains=affected,
            summary=self._summary(classification, open_causes, len(ruled_out), len(missing)),
            facts=_unique([*triage.available_evidence, *(e for d in diagnoses for e in d.evidence)]),
            probable_causes=[*open_causes, *ruled_out],
            supporting_evidence=_unique(e for c in open_causes for e in c.supporting_evidence),
            conflicting_evidence=_unique(e for c in causes for e in c.conflicting_evidence),
            missing_information=missing,
            recommended_checks=checks,
            recommended_actions=[a for d in diagnoses for a in d.recommended_actions],
            cross_domain_relationships=self._relationships(diagnoses),
            confidence=open_causes[0].confidence if open_causes else 0.0,
        )

    @staticmethod
    def _relationships(diagnoses: Sequence[AgentDiagnosis]) -> list[str]:
        relationships = []
        reported: dict[tuple[str, str], list[str]] = {}
        for d in diagnoses:
            for e in d.evidence:
                if e.kind is EvidenceKind.INCIDENT_REPORT:
                    reported.setdefault((e.source, e.statement), []).append(d.domain.value)
        for (source, statement), domains in reported.items():
            if len(set(domains)) >= 2:
                relationships.append(
                    f"Reported item '{statement}' <{source}> concerns {' and '.join(dedupe(domains))}; "
                    "these findings may describe the same event from different domains"
                )
        components: dict[str, list[str]] = {}
        for d in diagnoses:
            for component in d.affected_components:
                components.setdefault(component, []).append(d.domain.value)
        for component, domains in components.items():
            if len(set(domains)) >= 2:
                relationships.append(f"{component} is implicated by {' and '.join(dedupe(domains))} findings")
        return relationships

    @staticmethod
    def _cross_domain_checks(diagnoses: Sequence[AgentDiagnosis]) -> list[str]:
        examined = {d.domain for d in diagnoses}
        nodes = dedupe(
            [c.split(":", 1)[1] for d in diagnoses if d.hypotheses for c in d.affected_components if c.startswith("node:")]
        )
        return [
            f"{tool}: {domain} domain not examined in this run (node {node} implicated)"
            for node in nodes
            for domain, tool in _NODE_CHECKS.items()
            if domain not in examined
        ]

    @staticmethod
    def _summary(classification: Domain, open_causes: list, ruled_out: int, missing: int) -> str:
        if not open_causes:
            return (
                f"Classification: {classification}. No hypothesis is supported by the available evidence; "
                f"root cause not determined. {missing} item(s) of missing information."
            )
        top = open_causes[0].confidence
        leaders = [c for c in open_causes if c.confidence == top]
        if len(leaders) == 1:
            lead = f"Leading hypothesis ({leaders[0].domain}, confidence {top:.2f}): {leaders[0].cause}."
        else:
            lead = f"{len(leaders)} hypotheses are tied at confidence {top:.2f}: " + "; ".join(
                f"({c.domain}) {c.cause}" for c in leaders
            ) + "."
        others = len(open_causes) - len(leaders)
        return (
            f"Classification: {classification}. {lead} {others} other open hypothesis(es), {ruled_out} ruled out. "
            f"Root cause NOT confirmed; human review required. {missing} item(s) of missing information."
        )
