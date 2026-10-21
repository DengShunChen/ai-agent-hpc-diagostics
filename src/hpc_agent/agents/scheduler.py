"""Scheduler Agent (PJM / Slurm).

Uses saved job and node status. A failed job does not by itself indicate a scheduler
fault: abnormal terminations are interpreted as open possibilities spanning the
application, the node and other domains.
"""

from __future__ import annotations

import re

from hpc_agent.agents.evidence import dedupe, incident_evidence, tool_evidence
from hpc_agent.models import AgentDiagnosis, Domain, Evidence, Incident
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
        evidence: list[Evidence] = incident_evidence(incident, Domain.SCHEDULER)
        missing: list[str] = []
        checks: list[str] = []
        causes: list[str] = []
        confidences: list[float] = []
        summary: list[str] = []
        abnormal = False
        allocated: set[str] = set()

        job = gateway.invoke("get_job_status", incident_id=incident.incident_id)
        data = job.data if job.status is ToolStatus.OK and isinstance(job.data, dict) else None
        if data is None:
            missing.append("job termination details (state, exit code, termination reason)")
            checks.append("get_job_status()")
        else:
            job_id = data.get("job_id") if isinstance(data.get("job_id"), str) else "job"
            state = data.get("state")
            if isinstance(state, str):
                evidence.append(tool_evidence(job, f"{job_id} state={state}"))
                summary.append(f"{job_id} {state}")
            else:
                missing.append("job state")

            exit_code = data.get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                evidence.append(tool_evidence(job, f"{job_id} exit_code={exit_code}"))
                summary.append(f"exit code {exit_code}")
                if exit_code > 128:
                    signal = exit_code - 128
                    causes.append(_SIGNAL_CAUSES.get(signal, f"Job terminated by signal {signal}"))
                    confidences.append(0.3)
                    abnormal = True
                elif exit_code > 0:
                    causes.append(
                        f"Application exited with code {exit_code}; the application's own error output "
                        "should be checked before suspecting the scheduler"
                    )
                    confidences.append(0.3)
                    checks.append("review job stdout/stderr for application errors")
            else:
                missing.append("job exit code")

            requested, elapsed = data.get("requested_walltime"), data.get("elapsed")
            req_s, el_s = _seconds(requested), _seconds(elapsed)
            if req_s is None or el_s is None:
                missing.append("job elapsed time and walltime limit")
            elif el_s >= req_s:
                evidence.append(tool_evidence(job, f"elapsed {elapsed} reached requested walltime {requested}"))
                causes.append("Job reached its walltime limit")
                confidences.append(0.5)
            else:
                evidence.append(
                    tool_evidence(job, f"elapsed {elapsed} is below requested walltime {requested}; "
                                       "walltime limit was not reached")
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
                evidence.append(
                    tool_evidence(node, f"node {name} state={state}" + (f" reason={reason}" if has_reason else ""))
                )
                if state.upper() in UNHEALTHY_NODE_STATES:
                    abnormal = True
                    summary.append(f"node {name} {state}")
                    if not has_reason:
                        missing.append(f"reason node {name} entered {state}")
                    causes.append(
                        f"Node {name} entered {state}; a node problem may have terminated the job. The node "
                        "state alone does not show whether the cause is hardware, GPU, network, storage or an "
                        "administrator action"
                    )
                    confidences.append(0.3)
                    checks.append(f"review node health checks and system logs for {name} around the failure time")

        if abnormal:
            missing.append("node memory / out-of-memory events at the failure time")
            missing.append("storage, network and GPU state on the job's nodes at the failure time")

        return AgentDiagnosis(
            agent=self.name,
            domain=Domain.SCHEDULER,
            finding=self._finding(evidence, data is not None, summary, causes),
            evidence=evidence,
            possible_causes=causes,
            recommended_checks=dedupe(checks),
            missing_information=dedupe(missing),
            confidence=max(confidences, default=0.0),
        )

    @staticmethod
    def _finding(evidence: list[Evidence], has_job_data: bool, summary: list[str], causes: list[str]) -> str:
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
