"""GPU Agent (saved DCGM-style data only).

Never runs DCGM or any GPU command. XID codes are interpreted only as "commonly
associated with" a category; a hardware hypothesis requires hardware-indicative data
(hardware-class XIDs or non-zero hardware counters), and is never a conclusion.
"""

from __future__ import annotations

import re
from enum import StrEnum

from hpc_agent.agents.evidence import dedupe, gather_references, incident_evidence, number, timestamp, tool_evidence
from hpc_agent.models import AgentDiagnosis, Domain, Evidence, Incident, ProbableCause, RecommendedAction
from hpc_agent.tools.gateway import ScopedGateway, ToolStatus


class XidClass(StrEnum):
    APPLICATION = "application"
    HARDWARE = "hardware"
    FOLLOW_ON = "follow-on"


# Subset of NVIDIA XID codes with their commonly associated category.
XID_TABLE: dict[int, tuple[str, XidClass]] = {
    13: ("graphics engine exception", XidClass.APPLICATION),
    31: ("GPU memory page fault", XidClass.APPLICATION),
    43: ("GPU stopped processing", XidClass.APPLICATION),
    45: ("preemptive cleanup after a prior error", XidClass.FOLLOW_ON),
    48: ("double-bit ECC error", XidClass.HARDWARE),
    63: ("ECC page retirement or row remapping event", XidClass.HARDWARE),
    64: ("ECC page retirement or row remapper failure", XidClass.HARDWARE),
    74: ("NVLink error", XidClass.HARDWARE),
    79: ("GPU has fallen off the bus", XidClass.HARDWARE),
    92: ("high single-bit ECC error rate", XidClass.HARDWARE),
    94: ("contained ECC error", XidClass.HARDWARE),
    95: ("uncontained ECC error", XidClass.HARDWARE),
}

TELEMETRY = {
    "ecc_dbe_volatile": "ECC double-bit error counter",
    "temperature_c": "temperature reading",
    "power_w": "power reading",
    "pcie_replay_count": "PCIe replay counter",
    "nvlink_errors": "NVLink error counter",
}
THERMAL_LIMIT_C = 85.0
_CUDA_APP_ERROR = re.compile(r"illegal memory access|out of bounds|misaligned address|cuda error", re.IGNORECASE)


class GpuAgent:
    name = "gpu-agent"

    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis:
        reported = incident_evidence(incident, Domain.GPU)
        cuda_reports = [e for e in reported if _CUDA_APP_ERROR.search(e.statement)]
        evidence: list[Evidence] = list(reported)
        missing: list[str] = []
        checks: list[str] = []
        components: list[str] = []
        causes: list[ProbableCause] = []
        actions: list[RecommendedAction] = []
        summary: list[str] = []
        telemetry_gaps = False

        def hypothesis(text: str, confidence: float, support: list[Evidence]) -> None:
            causes.append(
                ProbableCause(cause=text, domain=Domain.GPU, supporting_evidence=support, confidence=confidence)
            )

        health = gateway.invoke("get_gpu_health", incident_id=incident.incident_id)
        data = health.data if health.status is ToolStatus.OK and isinstance(health.data, dict) else None
        gpus = data.get("gpus") if data else None
        if not isinstance(gpus, list):
            missing.append("saved GPU health / DCGM-style diagnostic data")
            checks.append("get_gpu_health()")
        else:
            node = data.get("node") if isinstance(data.get("node"), str) else incident.node or "unknown-node"
            for gpu in gpus:
                index = gpu.get("index") if isinstance(gpu, dict) else None
                if not isinstance(index, int) or isinstance(index, bool):
                    continue
                label = f"{node} GPU {index}"
                state = gpu.get("health")
                xids = [
                    (int(e["xid"]), int(number(e.get("count")) or 1), timestamp(e.get("timestamp")))
                    for e in gpu.get("xid_events") or []
                    if isinstance(e, dict) and number(e.get("xid")) is not None
                ]
                if not isinstance(state, str):
                    missing.append(f"{label} health status")
                if (not isinstance(state, str) or state.lower() == "pass") and not xids:
                    continue  # nothing reported against this GPU

                components += [f"node:{node}", f"gpu:{node}/{index}"]
                causes_before = len(causes)
                state_ev = []
                if isinstance(state, str):
                    state_ev = [tool_evidence(health, f"{label} health={state}")]
                    evidence += state_ev
                    summary.append(f"{label} health={state}")

                by_class: dict[XidClass, list[tuple[int, Evidence]]] = {}
                for xid, count, seen in xids:
                    item = tool_evidence(health, f"{label} XID {xid} count={count}", seen)
                    evidence.append(item)
                    summary.append(f"XID {xid}")
                    if xid in XID_TABLE:
                        by_class.setdefault(XID_TABLE[xid][1], []).append((xid, item))
                    else:
                        missing.append(f"interpretation of XID {xid} (not in the agent's XID table)")

                hw_support: list[Evidence] = []
                gaps = []
                for field, description in TELEMETRY.items():
                    value = number(gpu.get(field))
                    if value is None:
                        gaps.append(description)
                        continue
                    item = tool_evidence(health, f"{label} {field}={value:g}")
                    evidence.append(item)
                    if field == "temperature_c" and value >= THERMAL_LIMIT_C:
                        hypothesis(f"Thermal condition on {label} may have contributed to the error", 0.4, [item])
                    elif field == "ecc_dbe_volatile" and value > 0:
                        hw_support.append(item)
                    elif field in ("pcie_replay_count", "nvlink_errors") and value > 0:
                        hypothesis(
                            f"{description.capitalize()}s are non-zero on {label}; interconnect errors may have "
                            "contributed (significance depends on the site baseline)",
                            0.25,
                            [item],
                        )
                        missing.append(f"baseline {description} values for comparable GPUs")
                if gaps:
                    telemetry_gaps = True
                    missing.append(f"{label}: {', '.join(gaps)} not recorded")

                if app := by_class.get(XidClass.APPLICATION):
                    codes = ", ".join(f"XID {x} ({XID_TABLE[x][0]})" for x, _ in app)
                    hypothesis(
                        f"Application or CUDA software-environment fault on {label}: {codes} is commonly "
                        "associated with application errors rather than GPU hardware",
                        0.35,
                        [e for _, e in app] + cuda_reports,
                    )
                if hw := by_class.get(XidClass.HARDWARE):
                    hw_support += [e for _, e in hw]
                if hw_support:
                    hypothesis(
                        f"GPU hardware fault on {label} is indicated by saved data; operator-run diagnostics are "
                        "needed before any hardware conclusion",
                        0.5,
                        hw_support,
                    )
                    actions.append(
                        RecommendedAction(
                            action=f"Drain {node} from scheduling pending operator-run GPU diagnostics",
                            reason="Saved GPU data contains hardware-indicative errors",
                            risk="medium",
                            affected_component=label,
                            supporting_evidence=hw_support,
                        )
                    )
                if follow := by_class.get(XidClass.FOLLOW_ON):
                    missing.append(f"events preceding {', '.join(f'XID {x}' for x, _ in follow)} on {label}")
                if len(causes) == causes_before and state_ev:
                    hypothesis(f"GPU health check reports a problem on {label} without a recorded cause", 0.2, state_ev)
                checks.append(f"request an operator-run DCGM diagnostic on {node} (not executed by this platform)")

        if cuda_reports or any(c.cause.startswith("Application or CUDA") for c in causes):
            missing += [
                "driver and CUDA versions",
                "whether the error reproduces on other GPUs or nodes",
                "application code path at the failing CUDA call",
            ]
            checks += [
                "re-run the job on a different GPU or node to test reproducibility",
                "compare driver/CUDA versions with known-good nodes",
            ]

        references, reference_checks = gather_references(
            gateway, Domain.GPU, [c.cause for c in causes] or [e.statement for e in reported]
        )
        checks += reference_checks
        return AgentDiagnosis(
            agent=self.name,
            domain=Domain.GPU,
            finding=self._finding(evidence, isinstance(gpus, list), summary, causes, telemetry_gaps),
            evidence=evidence,
            possible_causes=causes,
            affected_components=dedupe(components),
            recommended_checks=dedupe(checks),
            recommended_actions=actions,
            missing_information=dedupe(missing),
            references=references,
            confidence=max((c.confidence for c in causes), default=0.0),
        )

    @staticmethod
    def _finding(
        evidence: list[Evidence], has_data: bool, summary: list[str], causes: list[ProbableCause], gaps: bool
    ) -> str:
        if not evidence:
            return ""
        if not has_data:
            return "No saved GPU health data is available; GPU involvement is not determined."
        if not summary:
            return "Saved GPU data shows all GPUs passing with no XID events; GPU involvement is not supported."
        observed = "; ".join(dedupe(summary))
        if any(c.cause.startswith("GPU hardware fault") for c in causes):
            return f"Saved GPU data shows {observed}, including hardware-indicative errors; not confirmed as the root cause."
        reason = "hardware telemetry is incomplete" if gaps else "no hardware fault indicators are present"
        return f"Saved GPU data shows {observed}. No hardware failure can be concluded: {reason}."
