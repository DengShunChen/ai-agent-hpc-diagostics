"""Storage Agent (FEFS / Lustre).

Uses saved filesystem and per-OST utilization. Reported "high utilization" is a claim,
not a measurement, and is never treated as proof that capacity caused a failure.
"""

from __future__ import annotations

import re

from hpc_agent.agents.evidence import dedupe, incident_evidence, number, tool_evidence
from hpc_agent.models import AgentDiagnosis, Domain, Evidence, Incident, RecommendedAction
from hpc_agent.tools.gateway import ScopedGateway, ToolStatus

FULL_PCT = 95.0
IMBALANCE_SPREAD_PCT = 20.0

_WRITE_FAILURE = re.compile(r"\b(writ(e|es|ing)|no space left|enospc|quota)\b", re.IGNORECASE)
_REPORTED_HIGH = re.compile(r"\b(utili[sz]ation|usage|capacity)\b.*\bhigh\b|\bfull\b", re.IGNORECASE)
_NO_SPACE = re.compile(r"no space left on device|enospc|quota exceeded", re.IGNORECASE)
_IO_ERROR = re.compile(r"\bi/o errors?\b", re.IGNORECASE)


class StorageAgent:
    name = "storage-agent"

    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis:
        reported = incident_evidence(incident, Domain.STORAGE)
        write_failure = any(_WRITE_FAILURE.search(e.statement) for e in reported)
        reported_high = any(_REPORTED_HIGH.search(e.statement) for e in reported)
        enospc_logged = any(_NO_SPACE.search(e.statement) for e in reported)
        io_errors = [e for e in reported if _IO_ERROR.search(e.statement)]

        evidence: list[Evidence] = list(reported)
        missing: list[str] = []
        checks: list[str] = []
        capacity: list[Evidence] = []  # tool evidence of a full filesystem or OST
        below_full: list[Evidence] = []  # tool evidence that the filesystem is not full
        imbalance: list[Evidence] = []
        unhealthy: list[Evidence] = []
        components: list[str] = []

        fs = gateway.invoke("get_filesystem_usage", incident_id=incident.incident_id)
        fs_data = fs.data if fs.status is ToolStatus.OK and isinstance(fs.data, dict) else None
        if fs_data is None:
            missing += ["actual filesystem utilization percentage", "filesystem health status"]
            checks.append("get_filesystem_usage()")
        else:
            name = fs_data.get("filesystem") if isinstance(fs_data.get("filesystem"), str) else "filesystem"
            used = number(fs_data.get("used_pct"))
            if used is None:
                missing.append("actual filesystem utilization percentage")
            else:
                item = tool_evidence(fs, f"{name} used_pct={used:g}")
                evidence.append(item)
                if used >= FULL_PCT:
                    capacity.append(item)
                    components.append(name)
                else:
                    below_full.append(item)
            health = fs_data.get("health")
            if not isinstance(health, str):
                missing.append("filesystem health status")
            else:
                item = tool_evidence(fs, f"{name} health={health}")
                evidence.append(item)
                if health.lower() != "healthy":
                    unhealthy.append(item)

        osts = gateway.invoke("get_ost_usage", incident_id=incident.incident_id)
        entries = osts.data.get("osts") if osts.status is ToolStatus.OK and isinstance(osts.data, dict) else None
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
                )
                evidence.append(item)
                capacity.append(item)
                components += [ost for ost, _ in full]
            low, high = min(p for _, p in measured), max(p for _, p in measured)
            item = tool_evidence(osts, f"OST utilization ranges {low:g}%-{high:g}% across {len(measured)} OSTs")
            evidence.append(item)
            if high - low >= IMBALANCE_SPREAD_PCT:
                imbalance.append(item)
        for ost, _, state in usage:
            if isinstance(state, str) and state.lower() != "active":
                item = tool_evidence(osts, f"{ost} state={state}")
                evidence.append(item)
                unhealthy.append(item)

        causes: list[str] = []
        confidences: list[float] = []
        actions: list[RecommendedAction] = []
        if capacity:
            causes.append(
                "Full filesystem or OST capacity may have caused the write failure"
                if write_failure
                else "Filesystem or OST capacity is near full"
            )
            confidences.append(0.6 if write_failure else 0.4)
            if write_failure and not enospc_logged:
                missing.append("'No space left on device' (ENOSPC) or quota error in the job output")
            actions.append(
                RecommendedAction(
                    action=f"Review and free capacity on {', '.join(dedupe(components))} following the site storage SOP",
                    reason="Saved utilization data shows capacity at or above the full threshold",
                    risk="medium",
                    affected_component=", ".join(dedupe(components)),
                    supporting_evidence=capacity,
                )
            )
        elif write_failure and reported_high and not below_full:
            causes.append("Storage capacity or OST imbalance may be contributing to the write failure")
            confidences.append(0.25)
        if imbalance:
            causes.append(
                "OST imbalance may cause writes to fail on the fullest OSTs even while the filesystem has free space"
            )
            confidences.append(0.4 if write_failure else 0.3)
        if unhealthy:
            causes.append("Degraded filesystem or OST health may be causing I/O failures")
            confidences.append(0.5)
            checks.append("review OSS/MDS server logs and target states for the incident window")
        if io_errors:
            causes.append("Reported I/O errors may indicate a storage server, target or client communication fault")
            confidences.append(0.3)
            missing.append("server-side (OSS/MDS) logs for the incident window")

        return AgentDiagnosis(
            agent=self.name,
            domain=Domain.STORAGE,
            finding=self._finding(evidence, fs_data is not None or bool(measured), reported_high, causes),
            evidence=evidence,
            possible_causes=causes,
            recommended_checks=dedupe(checks),
            recommended_actions=actions,
            missing_information=dedupe(missing),
            confidence=max(confidences, default=0.0),
        )

    @staticmethod
    def _finding(evidence: list[Evidence], measured: bool, reported_high: bool, causes: list[str]) -> str:
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
