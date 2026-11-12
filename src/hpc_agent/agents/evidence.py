"""Evidence helpers shared by agents.

Every Evidence item points back to the incident field or saved tool result it came from.
Agents only restate what is there; they never fill gaps with assumed values.
"""

from __future__ import annotations

import re
from datetime import datetime

from hpc_agent.models import Domain, Evidence, EvidenceKind, Incident
from hpc_agent.tools.gateway import ScopedGateway, ToolNotAllowedError, ToolResult
from hpc_agent.tools.knowledge_tools import terms

# Keyword signals used to relate incident text to domains. Matching is a triage aid only:
# a keyword hit marks an item as relevant to a domain, never as proof of a fault.
DOMAIN_PATTERNS: dict[Domain, re.Pattern[str]] = {
    Domain.STORAGE: re.compile(
        r"\b(file ?systems?|lustre|fefs|osts?|mdts?|mds|oss|quota|disk|i/o errors?|"
        r"no space left|writ(e|es|ing)|read failures?|stripe)\b",
        re.IGNORECASE,
    ),
    Domain.SCHEDULER: re.compile(
        r"\b(jobs?|scheduler|slurm|pjm|queued?|walltime|drain(ed|ing)?|exit code|"
        r"terminated|preempt\w*|allocation)\b",
        re.IGNORECASE,
    ),
    Domain.NETWORK: re.compile(
        r"\b(network|nic|bond\d*|link (down|flap\w*)|interfaces?|packet loss|packets?|"
        r"switch(es)?|router|infiniband|ethernet|snmp|unreachable)\b",
        re.IGNORECASE,
    ),
    Domain.GPU: re.compile(r"\b(gpus?|cuda|xid|dcgm|nvlink|pcie|ecc|nvidia|nccl)\b", re.IGNORECASE),
}

_INCIDENT_FIELDS = ("symptoms", "alerts", "logs", "operator_notes")


def match_domains(text: str) -> set[Domain]:
    return {domain for domain, pattern in DOMAIN_PATTERNS.items() if pattern.search(text)}


def incident_evidence(incident: Incident, domain: Domain | None = None) -> list[Evidence]:
    """Reported items from the incident, optionally only those relevant to one domain."""
    items = []
    for field in _INCIDENT_FIELDS:
        for i, text in enumerate(getattr(incident, field)):
            if domain is None or domain in match_domains(text):
                items.append(
                    Evidence(
                        kind=EvidenceKind.INCIDENT_REPORT,
                        source=f"{incident.incident_id}:{field}[{i}]",
                        statement=text,
                    )
                )
    return items


def tool_evidence(result: ToolResult, statement: str, observed_at: datetime | None = None) -> Evidence:
    return Evidence(
        kind=EvidenceKind.TOOL_RESULT,
        source=f"tool:{result.tool}:{result.source}",
        statement=statement,
        observed_at=observed_at,
    )


def timestamp(value: object) -> datetime | None:
    """A timezone-aware ISO 8601 timestamp, or None. Naive times are not guessed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


REFERENCE_TOOLS = (("search_sop", 2), ("search_incident_history", 2), ("search_manual", 1))
MAX_QUERY_TERMS = 12


def gather_references(gateway: ScopedGateway, domain: Domain, texts: list[str]) -> tuple[list[Evidence], list[str]]:
    """Look up knowledge relevant to the agent's own findings.

    Returns knowledge references and "consult SOP" checks. References are context for the
    human reviewer; they never become evidence or change confidence.
    """
    query_terms = dedupe([t for text in [domain.value, *texts] for t in terms(text)])[:MAX_QUERY_TERMS]
    if not texts or len(query_terms) < 2:
        return [], []
    query = " ".join(query_terms)
    references, checks = [], []
    for tool, limit in REFERENCE_TOOLS:
        if tool not in gateway.allowed_tools:
            continue
        try:
            hits = gateway.invoke(tool, query=query).data or []
        except ToolNotAllowedError:  # the run's call budget is spent; references are optional context
            checks.append("knowledge lookup skipped: the run's tool call budget was exhausted")
            break
        for hit in hits[:limit]:
            references.append(
                Evidence(
                    kind=EvidenceKind.KNOWLEDGE,
                    source=f"knowledge:{hit['source']}",
                    statement=f"{hit['doc_id']} ({hit['title']}): {hit['excerpt']}",
                )
            )
            if tool == "search_sop":
                checks.append(f"consult {hit['doc_id']} '{hit['title']}' (reference only)")
    return references, checks


def number(value: object) -> float | None:
    """A JSON number, or None. Booleans and strings are not treated as numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
