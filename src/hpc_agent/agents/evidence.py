"""Evidence helpers shared by agents.

Every Evidence item points back to the incident field or saved tool result it came from.
Agents only restate what is there; they never fill gaps with assumed values.
"""

from __future__ import annotations

import re

from hpc_agent.models import Domain, Evidence, EvidenceKind, Incident
from hpc_agent.tools.gateway import ToolResult

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


def tool_evidence(result: ToolResult, statement: str) -> Evidence:
    return Evidence(
        kind=EvidenceKind.TOOL_RESULT, source=f"tool:{result.tool}:{result.source}", statement=statement
    )


def number(value: object) -> float | None:
    """A JSON number, or None. Booleans and strings are not treated as numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
