"""Network Agent.

Uses saved interface / bond status and error counters only. It never contacts switches,
routers or SNMP agents. A single counter snapshot cannot show whether errors are
increasing, so counter-based findings stay hypotheses.
"""

from __future__ import annotations

from hpc_agent.agents.evidence import dedupe, incident_evidence, number, tool_evidence
from hpc_agent.models import AgentDiagnosis, Domain, Evidence, Incident, ProbableCause
from hpc_agent.tools.gateway import ScopedGateway, ToolStatus


class NetworkAgent:
    name = "network-agent"

    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis:
        evidence: list[Evidence] = incident_evidence(incident, Domain.NETWORK)
        missing: list[str] = []
        checks: list[str] = []
        components: list[str] = []
        causes: list[ProbableCause] = []

        def hypothesis(text: str, confidence: float, support: list[Evidence]) -> None:
            causes.append(
                ProbableCause(cause=text, domain=Domain.NETWORK, supporting_evidence=support, confidence=confidence)
            )

        status = gateway.invoke("get_network_status", incident_id=incident.incident_id)
        entries = (
            status.data.get("interfaces")
            if status.status is ToolStatus.OK and isinstance(status.data, dict)
            else None
        )
        if not isinstance(entries, list):
            missing.append("interface status and error counters for the affected node(s)")
            checks.append("get_network_status()")
        else:
            for entry in entries:
                if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("node", "interface")):
                    continue
                node, iface = entry["node"], entry["interface"]
                label = f"{node}/{iface}"
                implicated = False

                state = entry.get("state")
                if not isinstance(state, str):
                    missing.append(f"link state of {label}")
                else:
                    item = tool_evidence(status, f"{label} state={state}")
                    evidence.append(item)
                    if state.lower() != "up":
                        implicated = True
                        hypothesis(
                            f"Interface {label} is {state}; loss of connectivity on {node} may have disrupted "
                            "jobs, storage access or inter-node traffic",
                            0.5,
                            [item],
                        )

                down_members = [
                    m["interface"]
                    for m in entry.get("members") or []
                    if isinstance(m, dict) and isinstance(m.get("interface"), str)
                    and isinstance(m.get("state"), str) and m["state"].lower() != "up"
                ]
                if down_members and isinstance(state, str) and state.lower() == "up":
                    implicated = True
                    item = tool_evidence(status, f"{label} members not up: {', '.join(down_members)}")
                    evidence.append(item)
                    hypothesis(
                        f"Bond {label} is degraded ({', '.join(down_members)} not up); redundancy is reduced, "
                        "which alone does not indicate an outage",
                        0.2,
                        [item],
                    )

                rx, tx = number(entry.get("rx_errors")), number(entry.get("tx_errors"))
                if rx is None or tx is None:
                    missing.append(f"error counters for {label}")
                elif rx + tx > 0:
                    implicated = True
                    item = tool_evidence(status, f"{label} rx_errors={rx:g} tx_errors={tx:g}")
                    evidence.append(item)
                    hypothesis(
                        f"Interface {label} reports packet errors; possible cabling, optics or switch-port problem",
                        0.3,
                        [item],
                    )
                    missing.append(
                        f"error counter trend for {label} (a single snapshot cannot show whether errors are increasing)"
                    )

                flaps = number(entry.get("link_flaps"))
                if flaps is not None and flaps > 0:
                    implicated = True
                    item = tool_evidence(status, f"{label} link_flaps={flaps:g}")
                    evidence.append(item)
                    hypothesis(f"Link {label} flapped; intermittent connectivity is possible", 0.35, [item])

                if implicated:
                    components += [f"node:{node}", f"interface:{label}"]
                    missing.append(f"switch-side port status for {label}")
                    checks.append(f"review saved switch-port counters and logs for {label}")

        return AgentDiagnosis(
            agent=self.name,
            domain=Domain.NETWORK,
            finding=self._finding(evidence, isinstance(entries, list), causes),
            evidence=evidence,
            possible_causes=causes,
            affected_components=dedupe(components),
            recommended_checks=dedupe(checks),
            missing_information=dedupe(missing),
            confidence=max((c.confidence for c in causes), default=0.0),
        )

    @staticmethod
    def _finding(evidence: list[Evidence], has_data: bool, causes: list[ProbableCause]) -> str:
        if not evidence:
            return ""
        if not has_data:
            return "No saved network status is available; network involvement is not determined."
        if causes:
            return f"Saved network status is consistent with {len(causes)} possible cause(s); none is confirmed."
        return "Saved network status shows interfaces up without errors; network involvement is not supported."
