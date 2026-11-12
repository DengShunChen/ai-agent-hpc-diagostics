"""Scheduler Agent (PJM / Slurm).

Uses saved job and node status. A failed job does not by itself indicate a scheduler
fault: abnormal terminations are interpreted as open possibilities spanning the
application, the node and other domains.
"""

from __future__ import annotations

import json
import re

from hpc_agent.agents.evidence import dedupe, gather_references, incident_evidence, timestamp, tool_evidence
from hpc_agent.models import AgentDiagnosis, Domain, Evidence, HypothesisStatus, Incident, ProbableCause
from hpc_agent.tools.gateway import ScopedGateway, ToolStatus

UNHEALTHY_NODE_STATES = frozenset({"DRAIN", "DRAINED", "DRAINING", "DOWN", "FAIL", "FAILING", "NOT_RESPONDING"})

_SIGNAL_CAUSES = {
    6: "Application aborted (SIGABRT); an application or runtime-library fault is more likely than a scheduler fault",
    7: "Application bus error (SIGBUS); possible application fault or a problem with memory-mapped files",
    9: "Job processes were killed (SIGKILL); possible causes include an out-of-memory kill, a scheduler or "
    "administrator kill, or a node failure",
    11: "Application segmentation fault (SIGSEGV); an application or environment fault is more likely than a "
    "scheduler fault",
    15: "Job received SIGTERM; possible causes include cancellation, preemption or limit enforcement by the scheduler",
}
_DURATION = re.compile(r"(?:(\d+)-)?(\d+):(\d{2}):(\d{2})")


def _seconds(value: object) -> int | None:
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    return ((int(days or 0) * 24 + int(hours)) * 60 + int(minutes)) * 60 + int(seconds)


class SchedulerAgent:
    name = "scheduler-agent"

    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis:
        reported = incident_evidence(incident, Domain.SCHEDULER)
        evidence: list[Evidence] = list(reported)
        missing: list[str] = []
        checks: list[str] = []
        causes: list[ProbableCause] = []
        components: list[str] = []
        summary: list[str] = []

        def hypothesis(
            category: str, text: str, confidence: float, support: list[Evidence], concerns: list[str] = ()
        ) -> None:
            causes.append(
                ProbableCause(
                    cause=text, domain=Domain.SCHEDULER, category=category,
                    supporting_evidence=support, confidence=confidence, components=list(concerns),
                )
            )

        abnormal = False
        allocated: set[str] = set()

        job = gateway.invoke("get_job_status", incident_id=incident.incident_id)
        data = job.data if job.status is ToolStatus.OK and isinstance(job.data, dict) else None
        if data is None:
            missing.append("job termination details (state, exit code, termination reason)")
            checks.append("get_job_status()")
        else:
            job_id = data.get("job_id") if isinstance(data.get("job_id"), str) else "job"
            job_components = [f"job:{job_id}"] if job_id != "job" else []
            components += job_components
            ended = timestamp(data.get("end_time"))
            state = data.get("state")
            state_ev: list[Evidence] = []
            if isinstance(state, str):
                state_ev = [tool_evidence(job, f"{job_id} state={state}", ended)]
                evidence += state_ev
                summary.append(f"{job_id} {state}")
            else:
                missing.append("job state")

            exit_code = data.get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                exit_ev = tool_evidence(job, f"{job_id} exit_code={exit_code}", ended)
                evidence.append(exit_ev)
                summary.append(f"exit code {exit_code}")
                if exit_code > 128:
                    signal = exit_code - 128
                    category = "application.crash" if signal in (6, 7, 11) else "scheduler.signal_termination"
                    hypothesis(category, _SIGNAL_CAUSES.get(signal, f"Job terminated by signal {signal}"), 0.3,
                               state_ev + [exit_ev], job_components)
                    abnormal = True
                elif exit_code > 0:
                    hypothesis(
                        "application.exit_code",
                        f"Application exited with code {exit_code}; the application's own error output "
                        "should be checked before suspecting the scheduler",
                        0.3,
                        state_ev + [exit_ev],
                        job_components,
                    )
                    checks.append("review job stdout/stderr for application errors")
            else:
                missing.append("job exit code")

            requested, elapsed = data.get("requested_walltime"), data.get("elapsed")
            req_s, el_s = _seconds(requested), _seconds(elapsed)
            if req_s is None or el_s is None:
                missing.append("job elapsed time and walltime limit")
            elif el_s >= req_s:
                item = tool_evidence(job, f"elapsed {elapsed} reached requested walltime {requested}")
                evidence.append(item)
                hypothesis("scheduler.walltime", "Job reached its walltime limit", 0.5, state_ev + [item], job_components)
            else:
                item = tool_evidence(job, f"elapsed {elapsed} is below requested walltime {requested}; "
                                          "walltime limit was not reached")
                evidence.append(item)
                causes.append(
                    ProbableCause(
                        cause="Job reached its walltime limit",
                        domain=Domain.SCHEDULER,
                        category="scheduler.walltime",
                        status=HypothesisStatus.RULED_OUT,
                        supporting_evidence=state_ev,
                        conflicting_evidence=[item],
                    )
                )

            reason = data.get("termination_reason")
            if isinstance(reason, str) and reason:
                evidence.append(tool_evidence(job, f"termination_reason={reason}"))
            else:
                missing.append("job termination reason (not recorded in scheduler data)")

            nodes = data.get("allocated_nodes")
            allocated = {n for n in nodes if isinstance(n, str)} if isinstance(nodes, list) else set()

        node = gateway.invoke("get_node_status", incident_id=incident.incident_id)
        entries = node.data.get("nodes") if node.status is ToolStatus.OK and isinstance(node.data, dict) else None
        if not isinstance(entries, list):
            missing.append("state of the node(s) that ran the job")
            checks.append("get_node_status()")
        else:
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("node"), str):
                    continue
                name, state, reason = entry["node"], entry.get("state"), entry.get("reason")
                relevant = name in allocated or name == incident.node
                if not isinstance(state, str) or not relevant:
                    continue
                has_reason = isinstance(reason, str) and bool(reason)
                node_ev = tool_evidence(
                    node,
                    f"node {name} state={state}" + (f" reason={reason}" if has_reason else ""),
                    timestamp(entry.get("state_changed_at")),
                )
                evidence.append(node_ev)
                if state.upper() in UNHEALTHY_NODE_STATES:
                    abnormal = True
                    components.append(f"node:{name}")
                    summary.append(f"node {name} {state}")
                    if not has_reason:
                        missing.append(f"reason node {name} entered {state}")
                    hypothesis(
                        "node.unhealthy",
                        f"Node {name} entered {state}; a node problem may have terminated the job. The node "
                        "state alone does not show whether the cause is hardware, GPU, network, storage or an "
                        "administrator action",
                        0.3,
                        [node_ev],
                        [f"node:{name}"],
                    )
                    checks.append(f"review node health checks and system logs for {name} around the failure time")

        if data is not None and "get_log_digest" in gateway.allowed_tools:
            evidence += self._log_digest(incident, gateway, checks)

        if abnormal:
            missing.append("node memory / out-of-memory events at the failure time")
            missing.append("storage, network and GPU state on the job's nodes at the failure time")

        open_causes = [c for c in causes if c.status is HypothesisStatus.HYPOTHESIS]
        references, reference_checks = gather_references(
            gateway, Domain.SCHEDULER, [c.cause for c in open_causes] or [e.statement for e in reported]
        )
        checks += reference_checks
        return AgentDiagnosis(
            agent=self.name,
            domain=Domain.SCHEDULER,
            finding=self._finding(evidence, data is not None, summary, open_causes),
            evidence=evidence,
            possible_causes=causes,
            affected_components=dedupe(components),
            recommended_checks=dedupe(checks),
            missing_information=dedupe(missing),
            references=references,
            confidence=max((c.confidence for c in open_causes), default=0.0),
        )

    @staticmethod
    def _log_digest(incident: Incident, gateway: ScopedGateway, checks: list[str]) -> list[Evidence]:
        """The warning and error lines on the job's nodes while it ran, quoted exactly as facts.

        They are observations for the reviewer, not a diagnosis: no hypothesis is made from them here.
        """
        result = gateway.invoke("get_log_digest", incident_id=incident.incident_id)
        if result.status is not ToolStatus.OK or not isinstance(result.data, dict):
            return []
        rows = result.data.get("rows") or []
        out = []
        for row in rows:
            where = f"log={row['log']}, " + (f"node={row['node']}, " if row.get("node") else "") + f"line={row['line']}"
            repeats = f" (logged {row['repeats']} times until {row['last_seen']})" if row.get("repeats", 1) > 1 else ""
            out.append(tool_evidence(result, f"{where}: text={json.dumps(row['text'])}{repeats}", timestamp(row["timestamp"])))
        if rows:
            other = result.data.get("elsewhere") or {}
            checks.append(f"start from the first warning and error lines on the job's nodes (facts quoting log lines); "
                          f"{other.get('lines', 0)} similar line(s) on {other.get('nodes', 0)} other node(s) in the same "
                          "window are background to rule out")
        return out

    @staticmethod
    def _finding(
        evidence: list[Evidence], has_job_data: bool, summary: list[str], causes: list[ProbableCause]
    ) -> str:
        if not evidence:
            return ""
        if not has_job_data:
            return (
                "A job failure was reported but no scheduler data is available; a failed job does not by "
                "itself indicate a scheduler fault, and scheduler involvement is not determined."
            )
        observed = "; ".join(summary) if summary else "job data present"
        if causes:
            return (
                f"Scheduler data shows {observed}. This is consistent with {len(causes)} possible cause(s); "
                "none is confirmed and the data does not establish why the job was terminated."
            )
        return f"Scheduler data shows {observed}; no abnormal termination is evident."
