"""Diagnostic workflow, orchestrated with Microsoft Agent Framework.

    Incident -> Triage Agent -> selected specialist agents -> RCA Coordinator -> DiagnosticRun
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
from datetime import UTC, datetime
from typing import Never, Protocol

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from hpc_agent.agents import GpuAgent, NetworkAgent, RCACoordinator, SchedulerAgent, StorageAgent, TriageAgent
from hpc_agent.config import load_agent_tool_scopes
from hpc_agent.models import (
    AgentDiagnosis,
    DiagnosticRun,
    EvidenceKind,
    HypothesisStatus,
    Incident,
    RCAResult,
    ToolInvocationRecord,
    TriageResult,
)
from hpc_agent.tools.gateway import ScopedGateway, ToolGateway


class Specialist(Protocol):
    name: str

    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis: ...


@dataclass(frozen=True)
class TriagedIncident:
    incident: Incident
    triage: TriageResult


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
    async def on_triage(self, message: TriagedIncident, ctx: WorkflowContext[Never, RCAResult]) -> None:
        self._triaged = message
        await self._maybe_finish(ctx)

    @handler
    async def on_diagnosis(self, message: AgentDiagnosis, ctx: WorkflowContext[Never, RCAResult]) -> None:
        self._diagnoses.append(message)
        await self._maybe_finish(ctx)

    async def _maybe_finish(self, ctx: WorkflowContext[Never, RCAResult]) -> None:
        if self._triaged is None:
            return
        selected = self._triaged.triage.selected_agents
        expected = [a for a in selected if a in self._implemented]
        if len(self._diagnoses) < len(expected):
            return
        by_agent = {d.agent: d for d in self._diagnoses}
        await ctx.yield_output(
            self._coordinator.correlate(
                self._triaged.incident,
                self._triaged.triage,
                [by_agent[a] for a in expected],
                [a for a in selected if a not in self._implemented],
            )
        )


def _select(message: TriagedIncident, target_ids: list[str]) -> list[str]:
    return [t for t in target_ids if t in message.triage.selected_agents]


def default_specialists() -> list[Specialist]:
    return [StorageAgent(), SchedulerAgent(), NetworkAgent(), GpuAgent()]


async def run_diagnosis_async(
    incident: Incident,
    gateway: ToolGateway,
    scopes: Mapping[str, frozenset[str]] | None = None,
    specialists: Sequence[Specialist] | None = None,
) -> DiagnosticRun:
    scopes = load_agent_tool_scopes() if scopes is None else scopes
    specialists = default_specialists() if specialists is None else specialists
    log: list[ToolInvocationRecord] = []
    start = datetime.now(UTC)

    implemented = frozenset(a.name for a in specialists)
    triage_executor = TriageExecutor(TriageAgent())
    coordinator = CoordinatorExecutor(RCACoordinator(), implemented)
    workers = [SpecialistExecutor(a, ScopedGateway(gateway, a.name, scopes, log)) for a in specialists]
    builder = (
        WorkflowBuilder(start_executor=triage_executor, output_from="all")
        .add_multi_selection_edge_group(triage_executor, workers, _select)
        .add_edge(triage_executor, coordinator)
    )
    for worker in workers:
        builder.add_edge(worker, coordinator)
    outputs = (await builder.build().run(incident)).get_outputs()

    triage = next(o for o in outputs if isinstance(o, TriageResult))
    by_agent = {o.agent: o for o in outputs if isinstance(o, AgentDiagnosis)}
    rcas = [o for o in outputs if isinstance(o, RCAResult)]
    if len(rcas) != 1:
        raise RuntimeError(f"expected exactly one RCA result, got {len(rcas)}")
    return DiagnosticRun(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        incident_id=incident.incident_id,
        start_time=start,
        end_time=datetime.now(UTC),
        triage=triage,
        diagnoses=[by_agent[a] for a in triage.selected_agents if a in by_agent],
        unavailable_agents=[a for a in triage.selected_agents if a not in implemented],
        rca=rcas[0],
        tools_invoked=log,
    )


def run_diagnosis(
    incident: Incident,
    gateway: ToolGateway,
    scopes: Mapping[str, frozenset[str]] | None = None,
    specialists: Sequence[Specialist] | None = None,
) -> DiagnosticRun:
    return asyncio.run(run_diagnosis_async(incident, gateway, scopes, specialists))


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
    lines.append("\nCROSS-DOMAIN RELATIONSHIPS")
    lines += [f"  - {r}" for r in rca.cross_domain_relationships] or ["  none"]
    lines.append("\nMISSING INFORMATION")
    lines += [f"  - {m}" for m in rca.missing_information] or ["  none"]
    lines.append("\nRECOMMENDED CHECKS")
    lines += [f"  - {c}" for c in rca.recommended_checks] or ["  none"]
    lines.append("\nRECOMMENDED ACTIONS (proposals only: require human approval, never executed)")
    lines += [
        f"  - {a.action} | risk={a.risk} | component={a.affected_component} | reason: {a.reason}"
        for a in rca.recommended_actions
    ] or ["  none"]
    lines.append("\nTOOLS INVOKED")
    lines += [f"  - {r.agent}: {r.tool} -> {r.result_status}" for r in run.tools_invoked] or ["  none"]
    return "\n".join(lines)
