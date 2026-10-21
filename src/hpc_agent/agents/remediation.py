"""Remediation Agent: proposes remediation for well-supported hypotheses.

Roles in the workflow: triage routes, specialists diagnose, the RCA Coordinator correlates,
and this agent proposes what a human could do next. It has no tools and executes nothing:
every proposal is a RecommendedAction that needs human approval, and approval only records
the decision.

A hypothesis gets a proposal only when all of these hold:
  * it is open (not ruled out) and backed by tool evidence;
  * its confidence is at least the site's remediation_min_confidence (floor 0.3);
  * the playbook has an entry for its category.
Every other open hypothesis is listed as deferred, with the reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hpc_agent.models import (
    AgentDiagnosis,
    DeferredHypothesis,
    EvidenceKind,
    HypothesisStatus,
    ProbableCause,
    RCAResult,
    RecommendedAction,
    RemediationPlan,
    RiskLevel,
)
from hpc_agent.models.site import DEFAULT_PROFILE, SiteProfile


@dataclass(frozen=True)
class Play:
    action: str  # may use {targets} and {node}
    reason: str
    risk: RiskLevel
    verification: tuple[str, ...]
    rollback: str | None
    reference: str


PLAYBOOK: dict[str, Play] = {
    "storage.capacity": Play(
        "Review and free capacity on {targets} following the site storage SOP",
        "Saved utilization data shows capacity at or above the site's full threshold",
        RiskLevel.MEDIUM,
        ("confirm 'No space left on device' (ENOSPC) or quota errors in the job output",
         "re-measure filesystem and per-OST utilization immediately before acting"),
        "Data moves follow change control; nothing is deleted without the data owner's approval",
        "SOP-STOR-001",
    ),
    "storage.ost_imbalance": Play(
        "Plan an OST rebalance or adjust striping for directories writing to {targets}",
        "Saved per-OST utilization shows an imbalance above the site's threshold",
        RiskLevel.MEDIUM,
        ("confirm which OSTs the failing job's files are striped on",),
        "Striping changes apply to new files only; restore the previous layout if needed",
        "SOP-STOR-001",
    ),
    "storage.health": Play(
        "Escalate {targets} to the storage team for OSS/MDS and target health review",
        "Saved data reports degraded filesystem or OST health",
        RiskLevel.LOW,
        ("review OSS/MDS logs for the incident window",),
        None,
        "SOP-STOR-001",
    ),
    "scheduler.walltime": Play(
        "Ask the owner of {targets} to raise the walltime request or add checkpointing",
        "Saved job data shows the job reached its walltime limit",
        RiskLevel.LOW,
        ("confirm the job's real runtime requirement with its owner",),
        None,
        "SOP-SCHED-001",
    ),
    "node.unhealthy": Play(
        "Keep {targets} out of service until the drain or down reason is identified; do not resume it yet",
        "Saved node state shows the node unhealthy around the failure",
        RiskLevel.LOW,
        ("obtain the drain reason from the scheduler and node health checks",),
        "Resume the node only after the cause is cleared and the resume is approved",
        "SOP-SCHED-001",
    ),
    "application.crash": Play(
        "Ask the owner of {targets} to review the application error output before resubmitting",
        "Saved job data shows the application crashed (signal), not a scheduler limit",
        RiskLevel.LOW,
        ("check whether the crash reproduces with the same input",),
        None,
        "SOP-SCHED-001",
    ),
    "application.exit_code": Play(
        "Ask the owner of {targets} to review the application error output before resubmitting",
        "Saved job data shows a non-zero application exit code",
        RiskLevel.LOW,
        ("check the job's stdout/stderr for the application error",),
        None,
        "SOP-SCHED-001",
    ),
    "network.link_down": Play(
        "Take {node} out of scheduling and escalate {targets} to the network team",
        "Saved interface data shows the link down",
        RiskLevel.MEDIUM,
        ("check the switch-side port status for the link",),
        "Return the node to service once the link is verified up",
        "SOP-NET-001",
    ),
    "network.bond_degraded": Play(
        "Schedule a cable, optic and switch-port inspection for {targets}",
        "Saved bond data shows a member link not up",
        RiskLevel.LOW,
        ("compare two error-counter snapshots to see whether the problem is ongoing",),
        None,
        "SOP-NET-001",
    ),
    "network.packet_errors": Play(
        "Schedule a cable, optic and switch-port inspection for {targets}",
        "Saved interface counters show packet errors",
        RiskLevel.LOW,
        ("compare two error-counter snapshots to see whether errors are increasing",),
        None,
        "SOP-NET-001",
    ),
    "network.link_flap": Play(
        "Schedule a cable, optic and switch-port inspection for {targets}",
        "Saved interface data shows link flaps",
        RiskLevel.LOW,
        ("check the switch-side logs for the flap times",),
        None,
        "SOP-NET-001",
    ),
    "gpu.hardware": Play(
        "Drain {node} from scheduling pending operator-run GPU diagnostics",
        "Saved GPU data contains hardware-indicative errors",
        RiskLevel.MEDIUM,
        ("run the operator-led DCGM diagnostic under change control",
         "collect ECC, thermal and power history for the GPU"),
        "Resume the node after the diagnostics pass",
        "SOP-GPU-001",
    ),
    "gpu.thermal": Play(
        "Ask facilities to check cooling and airflow for {node}",
        "Saved GPU telemetry shows a temperature at or above the site's limit",
        RiskLevel.LOW,
        ("compare with the temperature history of neighbouring nodes",),
        None,
        "SOP-GPU-001",
    ),
    "gpu.application": Play(
        "Ask the job owner to reproduce the error on another GPU or node for {targets}",
        "Saved XID data is commonly associated with application errors",
        RiskLevel.LOW,
        ("check driver and CUDA versions against known-good nodes",),
        None,
        "SOP-GPU-001",
    ),
    "gpu.interconnect": Play(
        "Request an operator-run GPU diagnostic for {targets}",
        "Saved counters show non-zero PCIe or NVLink errors",
        RiskLevel.LOW,
        ("compare the counters with the site baseline",),
        None,
        "SOP-GPU-001",
    ),
}


def _display(component: str) -> str:
    kind, _, value = component.partition(":")
    if kind == "gpu" and "/" in value:
        node, _, index = value.rpartition("/")
        return f"{node} GPU {index}"
    return value or component


class RemediationAgent:
    name = "remediation-agent"

    def __init__(self, profile: SiteProfile = DEFAULT_PROFILE) -> None:
        self.min_confidence = profile.remediation_min_confidence

    def plan(self, rca: RCAResult, diagnoses: Sequence[AgentDiagnosis] = ()) -> tuple[list[RecommendedAction], RemediationPlan]:
        actions: list[RecommendedAction] = []
        deferred: list[DeferredHypothesis] = []
        for cause in (c for c in rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS):
            reason = self._why_not(cause)
            if reason:
                deferred.append(DeferredHypothesis(cause=cause.cause, category=cause.category,
                                                   confidence=cause.confidence, reason=reason))
                continue
            action = self._action(cause)
            if all(a.action != action.action for a in actions):
                actions.append(action)
        if actions:
            summary = (f"{len(actions)} remediation proposal(s) for human approval; "
                       f"{len(deferred)} hypothesis(es) deferred until more evidence is collected.")
        else:
            summary = ("No remediation proposed: no hypothesis is both tool-backed and at or above the "
                       f"site's confidence threshold ({self.min_confidence:.2f}). Complete the recommended "
                       "checks first.")
        return actions, RemediationPlan(summary=summary, min_confidence=self.min_confidence, deferred=deferred)

    def _why_not(self, cause: ProbableCause) -> str | None:
        if not any(e.kind is EvidenceKind.TOOL_RESULT for e in cause.supporting_evidence):
            return "supported only by reported symptoms; no tool evidence yet"
        if cause.confidence < self.min_confidence:
            return f"confidence {cause.confidence:.2f} is below the site threshold {self.min_confidence:.2f}"
        if cause.category not in PLAYBOOK:
            return f"no playbook entry for {cause.category or 'uncategorised causes'}; cause still unknown"
        if not cause.components:
            return "no affected component identified"
        return None

    def _action(self, cause: ProbableCause) -> RecommendedAction:
        play = PLAYBOOK[cause.category]
        primary = [c for c in cause.components if not c.startswith("node:")] or cause.components
        nodes = [_display(c) for c in cause.components if c.startswith("node:")]
        targets = ", ".join(_display(c) for c in primary)
        node = nodes[0] if nodes else targets
        return RecommendedAction(
            action=play.action.format(targets=targets, node=node),
            reason=play.reason,
            risk=play.risk,
            affected_component=targets,
            supporting_evidence=[e for e in cause.supporting_evidence if e.kind is EvidenceKind.TOOL_RESULT],
            addresses=cause.category,
            verification=list(play.verification),
            rollback=play.rollback,
            reference=play.reference,
        )
