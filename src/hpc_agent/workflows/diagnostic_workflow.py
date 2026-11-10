"""Diagnostic workflow, orchestrated with Microsoft Agent Framework.

    Incident -> Triage Agent -> selected specialist agents -> RCA Coordinator
             -> Remediation Agent -> DiagnosticRun
    (Triage also sends to the coordinator, which then knows how many diagnoses to expect.)

The framework only routes messages; agents stay plain Python. Any exception in an agent
(including a denied tool call) propagates out of the run: the workflow fails closed.
The coordinator never confirms a root cause; the approval workflow arrives in Iteration 5.
"""

# No `from __future__ import annotations`: Agent Framework introspects handler annotations.

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Never, Protocol

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from hpc_agent.agents import GpuAgent, NetworkAgent, RCACoordinator, SchedulerAgent, StorageAgent, TriageAgent
from hpc_agent.agents.llm_agents import LLMSpecialist, LLMTriageAgent
from hpc_agent.agents.remediation import RemediationAgent
from hpc_agent.agents.triage import AGENT_FOR_DOMAIN
from hpc_agent.config import load_agent_tool_scopes, load_site_profile
from hpc_agent.llm.provider import ChatLLM
from hpc_agent.runtime.trace import TraceRecorder, no_trace
from hpc_agent.models import (
    AgentDiagnosis,
    DiagnosticRun,
    EvidenceKind,
    HypothesisStatus,
    Incident,
    RCAResult,
    RecommendedAction,
    RemediationPlan,
    ToolInvocationRecord,
    TriageResult,
)
from hpc_agent.models.site import SiteProfile
from hpc_agent.safety.guards import verify_run
from hpc_agent.tools.gateway import CallBudget, ScopedGateway, ToolGateway


class Specialist(Protocol):
    name: str

    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis: ...


@dataclass(frozen=True)
class TriagedIncident:
    incident: Incident
    triage: TriageResult


@dataclass(frozen=True)
class Correlated:
    rca: RCAResult
    diagnoses: tuple[AgentDiagnosis, ...]


@dataclass(frozen=True)
class Remediated:
    rca: RCAResult
    plan: RemediationPlan


class TriageExecutor(Executor):
    def __init__(self, agent: TriageAgent) -> None:
        super().__init__(id=agent.name)
        self._agent = agent

    @handler
    async def handle(self, incident: Incident, ctx: WorkflowContext[TriagedIncident, TriageResult]) -> None:
        result = self._agent.triage(incident)
        await ctx.yield_output(result)
        await ctx.send_message(TriagedIncident(incident, result))


class SpecialistExecutor(Executor):
    def __init__(self, agent: Specialist, gateway: ScopedGateway) -> None:
        super().__init__(id=agent.name)
        self._agent = agent
        self._gateway = gateway

    @handler
    async def handle(self, message: TriagedIncident, ctx: WorkflowContext[AgentDiagnosis, AgentDiagnosis]) -> None:
        diagnosis = self._agent.diagnose(message.incident, self._gateway)
        await ctx.yield_output(diagnosis)
        await ctx.send_message(diagnosis)


class CoordinatorExecutor(Executor):
    """Collects the diagnoses of the specialists triage selected, then correlates them."""

    def __init__(self, coordinator: RCACoordinator, implemented: frozenset[str]) -> None:
        super().__init__(id=coordinator.name)
        self._coordinator = coordinator
        self._implemented = implemented
        self._triaged: TriagedIncident | None = None
        self._diagnoses: list[AgentDiagnosis] = []

    @handler
    async def on_triage(self, message: TriagedIncident, ctx: WorkflowContext[Correlated]) -> None:
        self._triaged = message
        await self._maybe_finish(ctx)

    @handler
    async def on_diagnosis(self, message: AgentDiagnosis, ctx: WorkflowContext[Correlated]) -> None:
        self._diagnoses.append(message)
        await self._maybe_finish(ctx)

    async def _maybe_finish(self, ctx: WorkflowContext[Correlated]) -> None:
        if self._triaged is None:
            return
        selected = self._triaged.triage.selected_agents
        expected = [a for a in selected if a in self._implemented]
        if len(self._diagnoses) < len(expected):
            return
        by_agent = {d.agent: d for d in self._diagnoses}
        diagnoses = tuple(by_agent[a] for a in expected)
        rca = self._coordinator.correlate(
            self._triaged.incident,
            self._triaged.triage,
            list(diagnoses),
            [a for a in selected if a not in self._implemented],
        )
        await ctx.send_message(Correlated(rca, diagnoses))


class RemediationExecutor(Executor):
    """Adds remediation proposals (never executed) to the correlated RCA."""

    def __init__(self, agent: RemediationAgent) -> None:
        super().__init__(id=agent.name)
        self._agent = agent

    @handler
    async def handle(self, message: Correlated, ctx: WorkflowContext[Never, Remediated]) -> None:
        actions, plan = self._agent.plan(message.rca, message.diagnoses)
        merged: list[RecommendedAction] = list(message.rca.recommended_actions)
        merged += [a for a in actions if all(a.action != m.action for m in merged)]
        await ctx.yield_output(Remediated(message.rca.model_copy(update={"recommended_actions": merged}), plan))


def _select(message: TriagedIncident, target_ids: list[str]) -> list[str]:
    return [t for t in target_ids if t in message.triage.selected_agents]


def default_specialists(profile: SiteProfile, llm: ChatLLM | None = None,
                        trace: TraceRecorder | None = None) -> list[Specialist]:
    rules: list[Specialist] = [StorageAgent(profile), SchedulerAgent(), NetworkAgent(), GpuAgent(profile)]
    if llm is None:
        return rules
    domains = {a: d for d, a in AGENT_FOR_DOMAIN.items()}
    return [LLMSpecialist(agent, domains[agent.name], llm, profile,
                          emit=trace.for_agent(agent.name) if trace else no_trace) for agent in rules]


async def run_diagnosis_async(
    incident: Incident,
    gateway: ToolGateway,
    scopes: Mapping[str, frozenset[str]] | None = None,
    specialists: Sequence[Specialist] | None = None,
    profile: SiteProfile | None = None,
    llm: ChatLLM | None = None,
    trace: TraceRecorder | None = None,
) -> DiagnosticRun:
    """With `llm`, triage and the specialists are LLM-driven (rules check them and are the fallback).
    With `trace`, every LLM agent step is recorded there."""
    scopes = load_agent_tool_scopes() if scopes is None else scopes
    profile = load_site_profile() if profile is None else profile
    specialists = default_specialists(profile, llm, trace) if specialists is None else specialists
    log: list[ToolInvocationRecord] = []
    budget = CallBudget(profile.tool_call_budget)
    start = datetime.now(UTC)

    implemented = frozenset(a.name for a in specialists)
    triage_executor = TriageExecutor(
        LLMTriageAgent(llm, emit=trace.for_agent("triage-agent") if trace else no_trace) if llm is not None
        else TriageAgent())
    coordinator = CoordinatorExecutor(
        RCACoordinator(timedelta(minutes=profile.correlation_window_minutes)), implemented
    )
    remediation = RemediationExecutor(RemediationAgent(profile))
    workers = [SpecialistExecutor(a, ScopedGateway(gateway, a.name, scopes, log, budget)) for a in specialists]
    builder = (
        WorkflowBuilder(start_executor=triage_executor, output_from="all")
        .add_multi_selection_edge_group(triage_executor, workers, _select)
        .add_edge(triage_executor, coordinator)
    )
    for worker in workers:
        builder.add_edge(worker, coordinator)
    builder.add_edge(coordinator, remediation)
    outputs = (await builder.build().run(incident)).get_outputs()

    triage = next(o for o in outputs if isinstance(o, TriageResult))
    by_agent = {o.agent: o for o in outputs if isinstance(o, AgentDiagnosis)}
    results = [o for o in outputs if isinstance(o, Remediated)]
    if len(results) != 1:
        raise RuntimeError(f"expected exactly one remediated RCA result, got {len(results)}")
    run = DiagnosticRun(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        incident_id=incident.incident_id,
        start_time=start,
        end_time=datetime.now(UTC),
        triage=triage,
        diagnoses=[by_agent[a] for a in triage.selected_agents if a in by_agent],
        unavailable_agents=[a for a in triage.selected_agents if a not in implemented],
        rca=results[0].rca,
        remediation=results[0].plan,
        tools_invoked=log,
        site=profile.site_id,
        incident_reported_at=incident.timestamp,
    )
    verify_run(run, scopes)
    return run


def run_diagnosis(
    incident: Incident,
    gateway: ToolGateway,
    scopes: Mapping[str, frozenset[str]] | None = None,
    specialists: Sequence[Specialist] | None = None,
    profile: SiteProfile | None = None,
    llm: ChatLLM | None = None,
    trace: TraceRecorder | None = None,
) -> DiagnosticRun:
    return asyncio.run(run_diagnosis_async(incident, gateway, scopes, specialists, profile, llm, trace))


def _tag(kind: EvidenceKind) -> str:
    return {EvidenceKind.INCIDENT_REPORT: "reported", EvidenceKind.TOOL_RESULT: "tool"}.get(kind, "knowledge")


def render_report(run: DiagnosticRun) -> str:
    rca, t = run.rca, run.triage
    related = f" (related: {', '.join(t.related_domains)})" if t.related_domains else ""
    open_causes = [c for c in rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]
    ruled_out = [c for c in rca.probable_causes if c.status is HypothesisStatus.RULED_OUT]
    lines = [
        f"INCIDENT {run.incident_id}   run {run.run_id}",
        f"TRIAGE: {t.classification}{related}",
        f"RCA CLASSIFICATION: {rca.classification}   AFFECTED DOMAINS: {', '.join(rca.affected_domains) or 'none'}",
        f"AGENTS RUN: {', '.join(d.agent for d in run.diagnoses) or 'none'}",
        f"AGENTS NOT AVAILABLE: {', '.join(run.unavailable_agents) or 'none'}",
        f"ROOT CAUSE: {'CONFIRMED' if run.root_cause_determined else 'NOT CONFIRMED (human review required)'}",
        "",
        "SUMMARY",
        f"  {rca.summary}",
        "\nFACTS",
    ]
    lines += [f"  [{_tag(e.kind)}] {e.statement}   <{e.source}>" for e in rca.facts] or ["  none"]
    lines.append("\nFINDINGS")
    lines += [f"  {d.agent} (confidence {d.confidence:.2f}): {d.finding or 'no finding'}" for d in run.diagnoses]
    lines.append("\nAGENT REASONING (llm = model proposals that passed the deterministic checks)")
    for item, agent in [(t, "triage-agent"), *((d, d.agent) for d in run.diagnoses)]:
        lines.append(f"  {agent}: {item.reasoning}" + (f" ({item.reasoning_note})" if item.reasoning_note else ""))
    discarded = [(d.agent, p) for d in run.diagnoses for p in d.discarded_proposals]
    if discarded:
        lines.append("  discarded model proposals (not findings):")
        lines += [f"    - {agent}: {p}" for agent, p in discarded]
    lines.append("\nHYPOTHESES (ranked, unconfirmed)")
    for i, c in enumerate(open_causes, 1):
        lines.append(f"  {i}. [{c.domain}] ({c.confidence:.2f}) {c.cause}")
        lines += [f"       supported by: {e.statement}   <{e.source}>" for e in c.supporting_evidence]
        lines += [f"       conflicts with: {e.statement}   <{e.source}>" for e in c.conflicting_evidence]
    if not open_causes:
        lines.append("  none")
    lines.append("\nRULED OUT")
    for c in ruled_out:
        lines.append(f"  - [{c.domain}] {c.cause}")
        lines += [f"       contradicted by: {e.statement}   <{e.source}>" for e in c.conflicting_evidence]
    if not ruled_out:
        lines.append("  none")
    lines.append("\nTIMELINE (relative to incident time)" if run.rca.timeline else "\nTIMELINE")
    lines += [f"  {e.observed_at.isoformat()}  {e.statement}   <{e.source}>" for e in rca.timeline] or ["  none"]
    lines.append("\nCROSS-DOMAIN RELATIONSHIPS")
    lines += [f"  - {r}" for r in rca.cross_domain_relationships] or ["  none"]
    lines.append("\nMISSING INFORMATION")
    lines += [f"  - {m}" for m in rca.missing_information] or ["  none"]
    lines.append("\nRECOMMENDED CHECKS")
    lines += [f"  - {c}" for c in rca.recommended_checks] or ["  none"]
    lines.append("\nREMEDIATION PLAN (remediation-agent; proposals only: require human approval, never executed)")
    if run.remediation:
        lines.append(f"  {run.remediation.summary}")
    for a in rca.recommended_actions:
        lines.append(f"  - {a.action} | risk={a.risk} | component={a.affected_component} | reason: {a.reason}")
        lines += [f"       verify first: {v}" for v in a.verification]
        if a.rollback:
            lines.append(f"       rollback: {a.rollback}")
        if a.reference:
            lines.append(f"       reference: {a.reference}")
    for d in (run.remediation.deferred if run.remediation else []):
        lines.append(f"  deferred: {d.cause} ({d.confidence:.2f}): {d.reason}")
    if not rca.recommended_actions and not run.remediation:
        lines.append("  none")
    lines.append("\nREFERENCES (knowledge base: context only, not evidence about this incident)")
    lines += [f"  - {r.statement}   <{r.source}>" for r in rca.references] or ["  none"]
    lines.append("\nTOOLS INVOKED")
    lines += [f"  - {r.agent}: {r.tool} -> {r.result_status}" for r in run.tools_invoked] or ["  none"]
    return "\n".join(lines)
