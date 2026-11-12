"""Cascade: the rules first; the LLM agents only where the rules say how a job ended, not why.

The rule-based agents are fast and reliable on the faults they encode, and on a fault they don't encode they
still lead with what they can see: the job hit its time limit, was killed, or exited non-zero
(`outcome_categories` in config/agents/). Those say how the job ended, not which component caused it. Only
then does the cascade run the LLM agents, and only a cause they ground in tool data, in a category that is not
an outcome, may lead. The rules' hypotheses stay right below it, so a correct rules answer is never dropped.

Never escalated: a rules run with no tool-supported hypothesis (the rules report missing evidence, and the
LLM agents must not turn a correct refusal into an answer), or one that already names a cause.

The final record holds both runs' diagnoses and tool calls, so every fact traces to a call in the same run
(`verify_run`), and `cascade` says what happened. Nothing here is model-driven: the gate and the ordering are
fixed code; the model only proposes, checked as in any LLM run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from datetime import timedelta

from hpc_agent.agents import RCACoordinator
from hpc_agent.agents.remediation import RemediationAgent
from hpc_agent.config import load_agent_tool_scopes, load_site_profile
from hpc_agent.llm.provider import ChatLLM
from hpc_agent.models import EvidenceKind, HypothesisStatus, Incident, ProbableCause, RecommendedAction
from hpc_agent.models.run import CascadeRecord, DiagnosticRun
from hpc_agent.models.site import SiteProfile
from hpc_agent.runtime.definitions import load_definitions, outcome_categories
from hpc_agent.runtime.trace import TraceRecorder
from hpc_agent.safety.guards import verify_run
from hpc_agent.tools.gateway import ToolGateway
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis


def _supported(cause: ProbableCause) -> bool:
    return any(e.kind is EvidenceKind.TOOL_RESULT for e in cause.supporting_evidence)


def gate(run: DiagnosticRun, outcomes: frozenset[str]) -> tuple[bool, str, str | None]:
    """(escalate?, why, the category the rules led with)."""
    open_causes = [c for c in run.rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]
    if not open_causes:
        return False, "the rules found no supported hypothesis; their result is kept", None
    lead = open_causes[0]
    if not _supported(lead):
        return False, "the rules' lead has no tool evidence (insufficient evidence); their result is kept", lead.category
    if lead.category in outcomes:
        return True, f"the rules lead with {lead.category}, which says how the job ended, not what caused it", lead.category
    return False, f"the rules lead with {lead.category}, a cause; their result is kept", lead.category


def run_cascade(
    incident: Incident,
    gateway: ToolGateway,
    llm: ChatLLM,
    scopes: Mapping[str, frozenset[str]] | None = None,
    profile: SiteProfile | None = None,
    trace: TraceRecorder | None = None,
    outcomes: frozenset[str] | None = None,
) -> DiagnosticRun:
    scopes = load_agent_tool_scopes() if scopes is None else scopes
    profile = load_site_profile() if profile is None else profile
    outcomes = outcome_categories(load_definitions()) if outcomes is None else outcomes

    started = time.monotonic()
    rules = run_diagnosis(incident, gateway, scopes, profile=profile)
    rules_seconds = time.monotonic() - started
    escalate, reason, rules_lead = gate(rules, outcomes)
    if not escalate:
        return rules.model_copy(update={"cascade": CascadeRecord(
            escalated=False, reason=reason, rules_lead=rules_lead, rules_seconds=rules_seconds)})

    started = time.monotonic()
    ai = run_diagnosis(incident, gateway, scopes, profile=profile, llm=llm, trace=trace)
    ai_seconds = time.monotonic() - started
    # Causes the LLM agents grounded in tool data (not a rules fallback), in a category that names a cause.
    ai_causes = [c for d in ai.diagnoses if d.reasoning == "llm" for c in d.hypotheses
                 if c.category not in outcomes and _supported(c)]
    ai_categories = {c.category for c in ai_causes}
    rules_causes = {(c.category, c.cause) for d in rules.diagnoses for c in d.possible_causes}
    diagnoses = [*ai.diagnoses, *rules.diagnoses]  # both runs, each agent's diagnosis once per run

    if ai_causes:
        rca = RCACoordinator(timedelta(minutes=profile.correlation_window_minutes)).correlate(
            incident, ai.triage, diagnoses, ai.unavailable_agents,
            rank=lambda c: 0 if c.category in ai_categories else 1 if (c.category, c.cause) in rules_causes else 2)
        actions, plan = RemediationAgent(profile).plan(rca, diagnoses)
        merged: list[RecommendedAction] = list(rca.recommended_actions)
        merged += [a for a in actions if all(a.action != m.action for m in merged)]
        rca = rca.model_copy(update={"recommended_actions": merged})
        open_lead = next(c for c in rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS)
        record = CascadeRecord(escalated=True, reason=reason, rules_lead=rules_lead, ai_lead=open_lead.category,
                               rules_seconds=rules_seconds, ai_seconds=ai_seconds)
    else:
        rca, plan = rules.rca, rules.remediation
        record = CascadeRecord(escalated=True, reason=reason + "; the LLM agents found no checked cause, so the "
                               "rules' result is kept", rules_lead=rules_lead,
                               rules_seconds=rules_seconds, ai_seconds=ai_seconds)

    run = DiagnosticRun(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        incident_id=incident.incident_id,
        start_time=rules.start_time,
        end_time=ai.end_time,
        triage=ai.triage,
        diagnoses=diagnoses,
        unavailable_agents=ai.unavailable_agents,
        rca=rca,
        remediation=plan,
        tools_invoked=[*rules.tools_invoked, *ai.tools_invoked],
        site=rules.site,
        incident_reported_at=rules.incident_reported_at,
        cascade=record,
    )
    verify_run(run, scopes)
    return run
