"""Diagnostic workflow, orchestrated with Microsoft Agent Framework.

    Incident -> Triage Agent -> selected specialist agents -> DiagnosticRun

The framework only routes messages; agents stay plain Python. Any exception in an agent
(including a denied tool call) propagates out of the run: the workflow fails closed.
The RCA Coordinator (Iteration 3) and approval workflow (Iteration 5) are not part of this
iteration, so a run never reports a determined root cause.
"""

# No `from __future__ import annotations`: Agent Framework introspects handler annotations.

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Never, Protocol

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from hpc_agent.agents import SchedulerAgent, StorageAgent, TriageAgent
from hpc_agent.config import load_agent_tool_scopes
from hpc_agent.models import (
    AgentDiagnosis,
    DiagnosticRun,
    EvidenceKind,
    Incident,
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
    async def handle(self, message: TriagedIncident, ctx: WorkflowContext[Never, AgentDiagnosis]) -> None:
        await ctx.yield_output(self._agent.diagnose(message.incident, self._gateway))


def _select(message: TriagedIncident, target_ids: list[str]) -> list[str]:
    return [t for t in target_ids if t in message.triage.selected_agents]


def default_specialists() -> list[Specialist]:
    return [StorageAgent(), SchedulerAgent()]


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

    triage_executor = TriageExecutor(TriageAgent())
    workers = [SpecialistExecutor(a, ScopedGateway(gateway, a.name, scopes, log)) for a in specialists]
    workflow = (
        WorkflowBuilder(start_executor=triage_executor, output_from="all")
        .add_multi_selection_edge_group(triage_executor, workers, _select)
        .build()
    )
    outputs = (await workflow.run(incident)).get_outputs()

    triage = next(o for o in outputs if isinstance(o, TriageResult))
    by_agent = {o.agent: o for o in outputs if isinstance(o, AgentDiagnosis)}
    implemented = {a.name for a in specialists}
    return DiagnosticRun(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        incident_id=incident.incident_id,
        start_time=start,
        end_time=datetime.now(UTC),
        triage=triage,
        diagnoses=[by_agent[a] for a in triage.selected_agents if a in by_agent],
        unavailable_agents=[a for a in triage.selected_agents if a not in implemented],
        tools_invoked=log,
    )


def run_diagnosis(
    incident: Incident,
    gateway: ToolGateway,
    scopes: Mapping[str, frozenset[str]] | None = None,
    specialists: Sequence[Specialist] | None = None,
) -> DiagnosticRun:
    return asyncio.run(run_diagnosis_async(incident, gateway, scopes, specialists))


def render_report(run: DiagnosticRun) -> str:
    t = run.triage
    related = f" (related: {', '.join(t.related_domains)})" if t.related_domains else ""
    lines = [
        f"INCIDENT {run.incident_id}   run {run.run_id}",
        f"CLASSIFICATION: {t.classification}{related}",
        f"AGENTS RUN: {', '.join(d.agent for d in run.diagnoses) or 'none'}",
        f"AGENTS NOT AVAILABLE: {', '.join(run.unavailable_agents) or 'none'}",
        "ROOT CAUSE: NOT DETERMINED (no RCA Coordinator in this iteration)",
        "",
        "FACTS",
    ]
    for e in run.facts:
        tag = "reported" if e.kind is EvidenceKind.INCIDENT_REPORT else "tool" if e.kind is EvidenceKind.TOOL_RESULT else "knowledge"
        lines.append(f"  [{tag}] {e.statement}   <{e.source}>")
    lines.append("\nFINDINGS")
    for d in run.diagnoses:
        lines.append(f"  {d.agent} (confidence {d.confidence:.2f}): {d.finding or 'no finding'}")
    lines.append("\nHYPOTHESES (unconfirmed)")
    lines += [f"  - [{d.agent}] {c}" for d in run.diagnoses for c in d.possible_causes] or ["  none"]
    lines.append("\nMISSING INFORMATION")
    lines += [f"  - {m}" for m in run.missing_information] or ["  none"]
    lines.append("\nRECOMMENDED CHECKS")
    lines += [f"  - {c}" for c in run.recommended_checks] or ["  none"]
    lines.append("\nRECOMMENDED ACTIONS (proposals only: require human approval, never executed)")
    actions = [(d.agent, a) for d in run.diagnoses for a in d.recommended_actions]
    lines += [f"  - [{agent}] {a.action} | risk={a.risk} | component={a.affected_component}" for agent, a in actions] or ["  none"]
    lines.append("\nTOOLS INVOKED")
    lines += [f"  - {r.agent}: {r.tool} -> {r.result_status}" for r in run.tools_invoked] or ["  none"]
    return "\n".join(lines)
