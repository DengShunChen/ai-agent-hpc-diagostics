"""Storage Agent (FEFS / Lustre).

Uses saved filesystem and per-OST utilization. Reported "high utilization" is a claim,
not a measurement, and is never treated as proof that capacity caused a failure.
"""

from __future__ import annotations

import re

from hpc_agent.agents.evidence import dedupe, gather_references, incident_evidence, number, timestamp, tool_evidence
from hpc_agent.models import (
    AgentDiagnosis,
    Domain,
    Evidence,
    HypothesisStatus,
    Incident,
    ProbableCause,
)
from hpc_agent.models.site import DEFAULT_PROFILE, SiteProfile
from hpc_agent.tools.gateway import ScopedGateway, ToolStatus

_WRITE_FAILURE = re.compile(r"\b(writ(e|es|ing)|no space left|enospc|quota)\b", re.IGNORECASE)
_REPORTED_HIGH = re.compile(r"\b(utili[sz]ation|usage|capacity)\b.*\bhigh\b|\bfull\b", re.IGNORECASE)
_NO_SPACE = re.compile(r"no space left on device|enospc|quota exceeded", re.IGNORECASE)
_IO_ERROR = re.compile(r"\bi/o errors?\b", re.IGNORECASE)


class StorageAgent:
    name = "storage-agent"

    def __init__(self, profile: SiteProfile = DEFAULT_PROFILE) -> None:
        self.full_pct = profile.storage_full_pct
        self.imbalance_spread_pct = profile.ost_imbalance_spread_pct

    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis:
        FULL_PCT = self.full_pct  # noqa: N806 (site threshold)
        reported = incident_evidence(incident, Domain.STORAGE)
        write_reports = [e for e in reported if _WRITE_FAILURE.search(e.statement)]
        high_reports = [e for e in reported if _REPORTED_HIGH.search(e.statement)]
        enospc_logged = any(_NO_SPACE.search(e.statement) for e in reported)
        io_errors = [e for e in reported if _IO_ERROR.search(e.statement)]

        evidence: list[Evidence] = list(reported)
        missing: list[str] = []
        checks: list[str] = []
        components: list[str] = []
        capacity: list[Evidence] = []  # tool evidence of a full filesystem or OST
        full_components: list[str] = []
        imbalance_components: list[str] = []
        unhealthy_components: list[str] = []
        below_full: list[Evidence] = []  # tool evidence that capacity is not exhausted
        imbalance: list[Evidence] = []
        unhealthy: list[Evidence] = []

        fs = gateway.invoke("get_filesystem_usage", incident_id=incident.incident_id)
        fs_data = fs.data if fs.status is ToolStatus.OK and isinstance(fs.data, dict) else None
        if fs_data is None:
            missing += ["actual filesystem utilization percentage", "filesystem health status"]
            checks.append("get_filesystem_usage()")
        else:
            name = fs_data.get("filesystem") if isinstance(fs_data.get("filesystem"), str) else "filesystem"
            fs_time = timestamp(fs_data.get("collected_at"))
            used = number(fs_data.get("used_pct"))
            if used is None:
                missing.append("actual filesystem utilization percentage")
            else:
                item = tool_evidence(fs, f"{name} used_pct={used:g}", fs_time)
                evidence.append(item)
                if used >= FULL_PCT:
                    capacity.append(item)
                    full_components.append(f"filesystem:{name}")
                    components.append(f"filesystem:{name}")
                else:
                    below_full.append(item)
            health = fs_data.get("health")
            if not isinstance(health, str):
                missing.append("filesystem health status")
            else:
                item = tool_evidence(fs, f"{name} health={health}", fs_time)
                evidence.append(item)
                if health.lower() != "healthy":
                    unhealthy.append(item)
                    unhealthy_components.append(f"filesystem:{name}")
                    components.append(f"filesystem:{name}")

        osts = gateway.invoke("get_ost_usage", incident_id=incident.incident_id)
        ost_data = osts.data if osts.status is ToolStatus.OK and isinstance(osts.data, dict) else {}
        entries = ost_data.get("osts")
        ost_time = timestamp(ost_data.get("collected_at"))
        usage = [
            (e["ost"], number(e.get("used_pct")), e.get("state"))
            for e in entries or []
            if isinstance(e, dict) and isinstance(e.get("ost"), str)
        ]
        measured = [(ost, pct) for ost, pct, _ in usage if pct is not None]
        if not measured:
            missing.append("individual OST utilization")
            checks.append("get_ost_usage()")
        else:
            full = [(ost, pct) for ost, pct in measured if pct >= FULL_PCT]
            if full:
                item = tool_evidence(
                    osts,
                    f"OSTs at or above {FULL_PCT:g}% used: " + ", ".join(f"{o}={p:g}%" for o, p in full),
                    ost_time,
                )
                evidence.append(item)
                capacity.append(item)
                full_components += [f"ost:{ost}" for ost, _ in full]
                components += [f"ost:{ost}" for ost, _ in full]
            low, high = min(p for _, p in measured), max(p for _, p in measured)
            item = tool_evidence(
                osts, f"OST utilization ranges {low:g}%-{high:g}% across {len(measured)} OSTs", ost_time
            )
            evidence.append(item)
            if high - low >= self.imbalance_spread_pct:
                imbalance.append(item)
                imbalance_components.append(f"ost:{max(measured, key=lambda m: m[1])[0]}")
            if not full:
                below_full.append(item)
        for ost, _, state in usage:
            if isinstance(state, str) and state.lower() != "active":
                item = tool_evidence(osts, f"{ost} state={state}", ost_time)
                evidence.append(item)
                unhealthy.append(item)
                unhealthy_components.append(f"ost:{ost}")
                components.append(f"ost:{ost}")

        if write_reports and incident.job_id:
            components.append(f"job:{incident.job_id}")

        causes: list[ProbableCause] = []

        def hypothesis(
            category: str, text: str, confidence: float, support: list[Evidence],
            conflicts: list[Evidence] = (), concerns: list[str] = (),
        ) -> None:
            causes.append(
                ProbableCause(
                    cause=text, domain=Domain.STORAGE, category=category, supporting_evidence=support,
                    conflicting_evidence=list(conflicts), confidence=confidence, components=list(concerns),
                )
            )

        if capacity:
            hypothesis(
                "storage.capacity",
                "Full filesystem or OST capacity may have caused the write failure"
                if write_reports
                else "Filesystem or OST capacity is near full",
                0.6 if write_reports else 0.4,
                write_reports + capacity,
                concerns=dedupe(full_components),
            )
            if write_reports and not enospc_logged:
                missing.append("'No space left on device' (ENOSPC) or quota error in the job output")
        elif write_reports and fs_data is not None and measured and below_full:
            # Capacity was measured at both filesystem and OST level and is not exhausted.
            causes.append(
                ProbableCause(
                    cause="Storage capacity caused the write failure",
                    domain=Domain.STORAGE,
                    category="storage.capacity",
                    status=HypothesisStatus.RULED_OUT,
                    supporting_evidence=write_reports + high_reports,
                    conflicting_evidence=below_full,
                )
            )
        elif write_reports and high_reports:
            hypothesis(
                "storage.capacity",
                "Storage capacity or OST imbalance may be contributing to the write failure",
                0.25,
                write_reports + high_reports,
                below_full,
            )
        if imbalance:
            hypothesis(
                "storage.ost_imbalance",
                "OST imbalance may cause writes to fail on the fullest OSTs even while the filesystem has free space",
                0.4 if write_reports else 0.3,
                write_reports + imbalance,
                concerns=imbalance_components,
            )
        if unhealthy:
            hypothesis("storage.health", "Degraded filesystem or OST health may be causing I/O failures", 0.5, unhealthy,
                       concerns=unhealthy_components)
            checks.append("review OSS/MDS server logs and target states for the incident window")
        if io_errors:
            hypothesis(
                "storage.io_error",
                "Reported I/O errors may indicate a storage server, target or client communication fault",
                0.3,
                io_errors,
            )
            missing.append("server-side (OSS/MDS) logs for the incident window")

        open_causes = [c for c in causes if c.status is HypothesisStatus.HYPOTHESIS]
        references, reference_checks = gather_references(
            gateway, Domain.STORAGE, [c.cause for c in open_causes] or [e.statement for e in reported]
        )
        checks += reference_checks
        return AgentDiagnosis(
            agent=self.name,
            domain=Domain.STORAGE,
            finding=self._finding(evidence, fs_data is not None or bool(measured), bool(high_reports), open_causes),
            evidence=evidence,
            possible_causes=causes,
            affected_components=dedupe(components),
            recommended_checks=dedupe(checks),
            missing_information=dedupe(missing),
            references=references,
            confidence=max((c.confidence for c in open_causes), default=0.0),
        )

    @staticmethod
    def _finding(evidence: list[Evidence], measured: bool, reported_high: bool, causes: list[ProbableCause]) -> str:
        if not evidence:
            return ""
        if not measured:
            prefix = "Reported high utilization is not a measurement. " if reported_high else ""
            return (
                f"{prefix}No saved storage measurements are available; storage involvement is a "
                "hypothesis only and the root cause is not determined."
            )
        if causes:
            return (
                f"Storage data is consistent with {len(causes)} possible cause(s); "
                "none is confirmed as the root cause."
            )
        return "Storage data shows no capacity, balance or health problem; storage involvement is not supported."
