"""RCA Coordinator: correlate specialist findings into one RCAResult.

The coordinator ranks hypotheses and relates findings across domains. It never confirms
a root cause: in this POC, confirmation is a human decision made after reviewing the
evidence. It adds no facts of its own; every fact comes from the incident or a tool.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import timedelta

from hpc_agent.agents.evidence import dedupe
from hpc_agent.models import (
    AgentDiagnosis,
    Domain,
    Evidence,
    EvidenceKind,
    HypothesisStatus,
    Incident,
    ProbableCause,
    RCAResult,
    TriageResult,
)

# Observations this close to the incident time are treated as concurrent with it.
CORRELATION_WINDOW = timedelta(minutes=30)
# Applied to a hypothesis whose tool evidence was all observed after the window closed.
LATE_EVIDENCE_FACTOR = 0.5

# Checks suggested for a node implicated in one domain when another domain was not examined.
_NODE_CHECKS = {
    Domain.STORAGE: "get_filesystem_usage()",
    Domain.NETWORK: "get_network_status()",
    Domain.GPU: "get_gpu_health()",
}


def _offset(delta: timedelta) -> str:
    seconds = int(abs(delta.total_seconds()))
    side = "before" if delta.total_seconds() < 0 else "after"
    if seconds < 90:
        return f"{seconds} s {side}"
    if seconds < 7200:
        return f"{round(seconds / 60)} min {side}"
    return f"{seconds / 3600:.1f} h {side}"


def _first_per_source(items: Iterable[Evidence]) -> list[Evidence]:
    first: dict[str, Evidence] = {}
    for item in items:
        first.setdefault(item.source, item)
    return list(first.values())


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
        facts = _unique([*triage.available_evidence, *(e for d in diagnoses for e in d.evidence)])
        timeline = sorted((e for e in facts if e.observed_at is not None), key=lambda e: e.observed_at)
        temporal, adjusted = self._temporal(incident, timeline, causes)
        causes = [adjusted.get(id(c), c) for c in causes]
        # Stable sort: equal confidence keeps triage/agent order.
        open_causes = sorted(
            (c for c in causes if c.status is HypothesisStatus.HYPOTHESIS), key=lambda c: -c.confidence
        )
        ruled_out = [c for c in causes if c.status is HypothesisStatus.RULED_OUT]

        affected = dedupe([c.domain for c in open_causes if c.domain is not None])
        tool_backed = {
            c.domain for c in open_causes
            if c.domain is not None and any(e.kind is EvidenceKind.TOOL_RESULT for e in c.supporting_evidence)
        }
        # Tool evidence outranks keyword triage; triage decides only when no hypothesis is tool-backed.
        # (Single-domain rule added 2026-10-21 after evaluation cases EVAL-STOR-001/003 were
        # classified multi-domain from a keyword tie; see docs/evaluation.md.)
        if len(tool_backed) >= 2:
            classification = Domain.MULTI_DOMAIN
        elif len(tool_backed) == 1:
            classification = next(iter(tool_backed))
        else:
            classification = triage.classification

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
            facts=facts,
            probable_causes=[*open_causes, *ruled_out],
            supporting_evidence=_unique(e for c in open_causes for e in c.supporting_evidence),
            conflicting_evidence=_unique(e for c in causes for e in c.conflicting_evidence),
            missing_information=missing,
            recommended_checks=checks,
            recommended_actions=[a for d in diagnoses for a in d.recommended_actions],
            cross_domain_relationships=self._relationships(diagnoses) + temporal,
            timeline=timeline,
            references=_first_per_source(r for d in diagnoses for r in d.references),
            confidence=open_causes[0].confidence if open_causes else 0.0,
        )

    @staticmethod
    def _temporal(
        incident: Incident, timeline: list[Evidence], causes: list[ProbableCause]
    ) -> tuple[list[str], dict[int, ProbableCause]]:
        """Relate timestamped observations to the incident time; down-weight late-only support."""
        if not timeline:
            return [], {}
        if incident.timestamp is None:
            return [f"{len(timeline)} timestamped observation(s) cannot be related to the incident: "
                    "incident timestamp missing"], {}
        start, window = incident.timestamp, CORRELATION_WINDOW
        within = [e for e in timeline if abs(e.observed_at - start) <= window]
        after = [e for e in timeline if e.observed_at - start > window]
        before = [e for e in timeline if start - e.observed_at > window]
        minutes = int(window.total_seconds() // 60)
        notes = []
        if within:
            notes.append(f"Within {minutes} min of the incident: "
                         + "; ".join(f"{e.statement} ({_offset(e.observed_at - start)})" for e in within))
        if after:
            notes.append(f"Observed more than {minutes} min after the incident, so it cannot have caused it and "
                         "may not reflect conditions at failure time: "
                         + "; ".join(f"{e.statement} ({_offset(e.observed_at - start)})" for e in after))
        if before:
            notes.append(f"Observed more than {minutes} min before the incident (relevance uncertain): "
                         + "; ".join(f"{e.statement} ({_offset(e.observed_at - start)})" for e in before))

        adjusted = {}
        for cause in causes:
            tool = [e for e in cause.supporting_evidence if e.kind is EvidenceKind.TOOL_RESULT]
            if cause.status is HypothesisStatus.HYPOTHESIS and tool and all(
                e.observed_at is not None and e.observed_at - start > window for e in tool
            ):
                adjusted[id(cause)] = cause.model_copy(
                    update={"confidence": round(cause.confidence * LATE_EVIDENCE_FACTOR, 2)}
                )
                notes.append(f"Confidence of '{cause.cause}' halved: all of its tool evidence postdates the incident")
        return notes, adjusted

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
