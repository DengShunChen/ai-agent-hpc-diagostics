"""LLM-driven agents: the model reasons, deterministic checks decide what is kept.

An LLM specialist gets the incident and the read-only tools of its own scope (through its
ScopedGateway). The model decides which tools to call, reads the results and proposes
hypotheses. Each proposal then passes deterministic checks before a human can see it:

- every fact must cite a tool result this agent actually fetched, by field and value. The
  evidence statement is written from the real data, never from the model's words;
- components are derived from the cited data, not taken from the model;
- categories come from a fixed list; confidence is capped; nothing can be confirmed;
- text with numbers that appear nowhere in the data, commands, confirmation claims or
  credential-like content is discarded.

Discarded proposals are listed for the reviewer, never shown as findings. Tool arguments
are never taken from the model: data tools always get this incident's ID, and a knowledge
query must pass the gateway's argument pattern. If the model is unavailable or its answer
is unusable, the rule-based agent runs instead and the diagnosis says so.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from hpc_agent.agents.evidence import dedupe, gather_references, timestamp, tool_evidence
from hpc_agent.agents.triage import AGENT_FOR_DOMAIN, TriageAgent
from hpc_agent.llm.provider import ChatLLM, LLMUnavailableError
from hpc_agent.models import AgentDiagnosis, Domain, Evidence, EvidenceKind, HypothesisStatus, Incident, TriageResult
from hpc_agent.models.diagnosis import ProbableCause
from hpc_agent.models.site import DEFAULT_PROFILE, SiteProfile
from hpc_agent.runtime.context import incident_brief, incident_items
from hpc_agent.runtime.definitions import AgentDefinition, load_definitions
from hpc_agent.runtime.loop import ask_json, run_agent
from hpc_agent.runtime.tools import ToolSession, run_tool, tool_specs
from hpc_agent.safety.secrets import find_credential_like
from hpc_agent.tools.gateway import ScopedGateway

MAX_TEXT = 300
MAX_LIST_ITEMS = 6
CAP_WITH_TOOL_FACTS = 0.6
CAP_REPORTS_ONLY = 0.3

_COMMANDS = re.compile(
    r"`|\$\(|&&|\|\||\bsudo\b|\brm\s+-|\bssh\s|\bsystemctl\b|\bkill\s+-|\breboot\b|\bscontrol\b"
    r"|\blctl\b|\blfs\s|\bnvidia-smi\b|\bdcgmi\b|\bpdsh\b|\bchmod\b|\bdnf\b|\byum\b",
    re.IGNORECASE,
)
_CONFIRMED = re.compile(r"\b(confirmed|definitely|certainly|proven|the root cause is)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_CONTEXT_KEYS = ("filesystem", "ost", "job_id", "node", "interface", "index")
_TIME_KEYS = ("timestamp", "state_changed_at", "last_change", "end_time", "collected_at")
WITHHELD = "a proposal was withheld (SENSITIVE INFORMATION DETECTED AND NOT PROCESSED.)"


def safe_note(note: str) -> str:
    """A discard note may quote the model; never keep one that carries credential-like content."""
    return WITHHELD if find_credential_like(note) else note


# --- grounding --------------------------------------------------------------------------

@dataclass(frozen=True)
class Leaf:
    field: str
    value: Any
    context: tuple[tuple[str, str], ...]
    observed_at: datetime | None
    path: tuple[str, ...] = ()  # keys from the top of the result, e.g. ("gpus", "xid_events", "count")


def leaves(data: Any) -> list[Leaf]:
    """Every scalar in a tool result, with the identifiers and time of the record it sits in."""
    out: list[Leaf] = []

    def walk(node: Any, path: tuple[str, ...], context: tuple[tuple[str, str], ...], seen: datetime | None) -> None:
        if isinstance(node, dict):
            context = context + tuple(
                (k, str(node[k])) for k in _CONTEXT_KEYS
                if isinstance(node.get(k), (str, int)) and not isinstance(node.get(k), bool)
            )
            seen = next((t for k in _TIME_KEYS if (t := timestamp(node.get(k)))), seen)
            for k, v in node.items():
                walk(v, (*path, k), context, seen)
        elif isinstance(node, list):
            for item in node:
                walk(item, path, context, seen)
        else:
            out.append(Leaf(path[-1] if path else "", node, context, seen, path))

    walk(data, (), (), None)
    return out


_INDEX = re.compile(r"\[[^\]]*\]")
_ID_SEPARATORS = re.compile(r"[:/,\s]+")


def field_matches(cited: Any, leaf: Leaf) -> bool:
    """A leaf name ("count") or a dotted path ending at the leaf ("xid_events.count", "gpus[3].health").

    List indices are ignored; the record is identified by `where`, and the evidence statement
    always names the record that actually matched.
    """
    if not isinstance(cited, str):
        return False
    segments = tuple(s.strip() for s in _INDEX.sub("", cited).split(".") if s.strip())
    return bool(segments) and leaf.path[-len(segments):] == segments


def where_matches(where: Any, leaf: Leaf) -> bool:
    """No `where`, one record ID ("OST0002"), or a compound ID whose every part identifies the
    record ("sim-gpu-node-0007:3", "node=n1/interface=ib0")."""
    if not isinstance(where, str) or not where.strip():
        return True
    values = {v.lower() for _, v in leaf.context}
    if where.strip().lower() in values or _same(where, leaf.value):
        return True
    keys = {k.lower() for k, _ in leaf.context}
    parts = [p.split("=", 1)[-1].strip().lower() for p in _ID_SEPARATORS.split(where.strip()) if p.strip()]
    parts = [p for p in parts if p not in keys]  # "node sim-node-1": the key name is not an ID
    return bool(parts) and all(p in values for p in parts)


def _same(cited: Any, actual: Any) -> bool:
    if actual is None or isinstance(actual, bool) or isinstance(cited, bool):
        return cited == actual
    if isinstance(actual, (int, float)):
        try:
            return abs(float(cited) - float(actual)) < 1e-6
        except (TypeError, ValueError):
            return False
    return isinstance(cited, (str, int, float)) and str(cited).strip().lower() == str(actual).strip().lower()


def components_of(leaf: Leaf) -> list[str]:
    ctx = dict(leaf.context)
    node = ctx.get("node")
    out = []
    if "filesystem" in ctx:
        out.append(f"filesystem:{ctx['filesystem']}")
    if "ost" in ctx:
        out.append(f"ost:{ctx['ost']}")
    if "job_id" in ctx:
        out.append(f"job:{ctx['job_id']}")
    if node:
        out.append(f"node:{node}")
        if "index" in ctx:
            out.append(f"gpu:{node}/{ctx['index']}")
        if "interface" in ctx:
            out.append(f"interface:{node}/{ctx['interface']}")
    if leaf.field == "allocated_nodes" and isinstance(leaf.value, str):
        out.append(f"node:{leaf.value}")
    return out


def text_problem(text: str, allowed_numbers: set[str]) -> str | None:
    """Why a piece of model text can't be shown to a reviewer, or None."""
    if len(text) > MAX_TEXT:
        return "text too long"
    if _COMMANDS.search(text):
        return "contains a command"
    if _CONFIRMED.search(text):
        return "claims a confirmed cause"
    if find_credential_like(text):
        return "credential-like content"
    unknown = [n for n in _NUMBER.findall(text) if n not in allowed_numbers]
    if unknown:
        return f"numbers not found in the data: {', '.join(unknown[:3])}"
    return None


def _strings(value: Any) -> list[str]:
    return [v.strip() for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


# --- specialist ---------------------------------------------------------------------------

def site_thresholds(domain: Domain, profile: SiteProfile) -> str:
    """The site profile's thresholds for this domain: the same ones the rule-based agents apply."""
    lines = {
        Domain.STORAGE: [f"a filesystem or OST is full at used_pct >= {profile.storage_full_pct:g}",
                         f"OSTs are imbalanced when max(used_pct) - min(used_pct) >= {profile.ost_imbalance_spread_pct:g}"],
        Domain.GPU: [f"a GPU is too hot at temperature_c >= {profile.gpu_thermal_limit_c:g}"],
    }.get(domain, [])
    return ("Site thresholds (site " + profile.site_id + "): " + "; ".join(lines) + "."
            if lines else "Site thresholds: none for this domain.")


class LLMSpecialist:
    """Wraps a rule-based specialist: same name, scope and domain; the rules are the fallback.

    Its prompt, categories and limits come from its definition (config/agents/<name>.md); the
    tool loop is the shared harness (runtime/loop.py). What it may keep is decided here.
    """

    def __init__(self, rules: Any, domain: Domain, llm: ChatLLM, profile: SiteProfile = DEFAULT_PROFILE,
                 definition: AgentDefinition | None = None) -> None:
        self.name = rules.name
        self.domain = domain
        self._rules = rules
        self._llm = llm
        self._thresholds = site_thresholds(domain, profile)
        self._definition = definition or load_definitions()[self.name]
        if self._definition.llm is None:
            raise ValueError(f"{self.name} has no llm settings in its definition")
        self._categories = self._definition.llm.categories

    # The workflow calls this exactly like a rule-based specialist.
    def diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis:
        try:
            return self._diagnose(incident, gateway)
        except (LLMUnavailableError, _Unusable, ValidationError) as error:
            diagnosis = self._rules.diagnose(incident, gateway)
            return diagnosis.model_copy(update={"reasoning": "rules-fallback",
                                                "reasoning_note": f"{self._llm.model}: {error}"})

    def _diagnose(self, incident: Incident, gateway: ScopedGateway) -> AgentDiagnosis:
        reported, items = incident_items(incident, with_source=True)
        data_tools = ", ".join(t for t in sorted(gateway.allowed_tools) if not t.startswith("search_")) or "none"
        system = self._definition.render(domain=self.domain.value, categories=", ".join(self._categories),
                                         data_tools=data_tools, thresholds=self._thresholds)
        session = ToolSession(max_calls=self._definition.llm.max_tool_calls)
        answer = run_agent(
            self._llm, system, incident_brief(incident, items), tool_specs(gateway), self._definition.llm.max_rounds,
            lambda name, arguments: run_tool(name, arguments, incident, gateway, session, self.domain.value),
        )
        if answer is None:
            raise _Unusable("answer was not valid JSON")
        return self._check(incident, gateway, answer, reported, session)

    def _fact(self, cited: Any, s: ToolSession) -> tuple[Evidence | None, list[str], str | None]:
        if not isinstance(cited, dict):
            return None, [], "fact is not an object"
        ref = cited.get("ref")
        result = s.fetched.get(ref) if isinstance(ref, str) else None
        if result is None:
            return None, [], f"cites {ref!r}, which is not a tool result this agent fetched"
        name, where = cited.get("field"), cited.get("where")
        matches = [leaf for leaf in leaves(result.data)
                   if field_matches(name, leaf) and _same(cited.get("value"), leaf.value) and where_matches(where, leaf)]
        if not matches:
            return None, [], f"{ref} has no {name}={cited.get('value')!r}" + (f" for {where}" if where else "")
        leaf = matches[0]
        label = ", ".join(f"{k}={v}" for k, v in leaf.context)
        statement = f"{label}: {leaf.field}={json.dumps(leaf.value)}" if label else f"{leaf.field}={json.dumps(leaf.value)}"
        return tool_evidence(result, statement, leaf.observed_at), components_of(leaf), None

    def _check(self, incident: Incident, gateway: ScopedGateway, answer: dict, reported: list[Evidence],
               s: ToolSession) -> AgentDiagnosis:
        corpus = json.dumps([r.data for r in s.fetched.values()]) + " ".join(e.statement for e in reported)
        corpus += " " + incident.incident_id + " " + (incident.node or "") + " " + (incident.job_id or "")
        corpus += " " + (incident.timestamp.isoformat() if incident.timestamp else "") + " " + self._thresholds
        allowed_numbers = set(_NUMBER.findall(corpus))
        evidence: list[Evidence] = []
        causes: list[ProbableCause] = []
        components: list[str] = []
        discarded: list[str] = []

        def add(item: Evidence) -> Evidence:
            if item not in evidence:
                evidence.append(item)
            return item

        def proposal(raw: Any, status: HypothesisStatus) -> None:
            if not isinstance(raw, dict) or not isinstance(raw.get("cause"), str) or not raw["cause"].strip():
                discarded.append("a proposal without a cause")
                return
            cause = " ".join(raw["cause"].split())
            label = f"'{cause[:80]}'"
            problem = text_problem(cause, allowed_numbers)
            if problem == "credential-like content":
                discarded.append(WITHHELD)
                return
            if problem:
                discarded.append(f"{label}: {problem}")
                return
            category = raw.get("category")
            if category not in self._categories:
                discarded.append(f"{label}: category {category!r} is not one of {self.domain.value}'s categories")
                return
            facts, comps, problems = [], [], []
            report_refs = _strings(raw.get("reported"))
            for cited in raw.get("facts") or []:
                ref = cited.get("ref") if isinstance(cited, dict) else cited
                if isinstance(ref, str) and re.fullmatch(r"I\d+", ref.strip()):
                    report_refs.append(ref.strip())  # an incident item put under facts: treat it as reported
                    continue
                item, found, problem = self._fact(cited, s)
                if problem:
                    problems.append(problem)
                else:
                    facts.append(item)
                    comps += found
            if problems:
                discarded.append(f"{label}: {problems[0]}")
                return
            reports = []
            for ref in dedupe(report_refs):
                index = int(ref[1:]) if re.fullmatch(r"I\d+", ref) else 0
                if 1 <= index <= len(reported):
                    reports.append(reported[index - 1])
            if status is HypothesisStatus.RULED_OUT:
                if not facts:
                    discarded.append(f"{label}: ruling a cause out needs cited tool data")
                    return
                causes.append(ProbableCause(cause=cause, domain=self.domain, category=category, status=status,
                                            components=dedupe(comps), conflicting_evidence=[add(f) for f in facts]))
                return
            if not facts and not reports:
                discarded.append(f"{label}: no cited evidence")
                return
            cap = CAP_WITH_TOOL_FACTS if facts else CAP_REPORTS_ONLY
            try:
                confidence = min(max(float(raw.get("confidence") or 0.0), 0.0), cap)
            except (TypeError, ValueError):
                confidence = 0.0
            causes.append(ProbableCause(cause=cause, domain=self.domain, category=category,
                                        components=dedupe(comps), confidence=confidence or 0.1,
                                        supporting_evidence=[add(e) for e in [*facts, *reports]]))
            components.extend(comps)

        for raw in (answer.get("hypotheses") or [])[:MAX_LIST_ITEMS]:
            proposal(raw, HypothesisStatus.HYPOTHESIS)
        for raw in (answer.get("ruled_out") or [])[:MAX_LIST_ITEMS]:
            proposal(raw, HypothesisStatus.RULED_OUT)
        causes.sort(key=lambda c: (c.status is not HypothesisStatus.HYPOTHESIS, -c.confidence))

        def texts(key: str) -> list[str]:
            kept = []
            for text in _strings(answer.get(key))[:MAX_LIST_ITEMS]:
                problem = text_problem(text, allowed_numbers)
                if problem == "credential-like content":
                    discarded.append(WITHHELD)
                elif problem:
                    discarded.append(f"{key} '{text[:60]}': {problem}")
                else:
                    kept.append(text)
            return kept

        missing = [f"no saved data from {tool}" for tool in dedupe(s.no_data)]
        data_tools = [t for t in sorted(gateway.allowed_tools) if not t.startswith("search_")]
        consulted = {r.tool for r in s.fetched.values()} | set(s.no_data)
        missing += [f"{tool} was not consulted by the model" for tool in data_tools if tool not in consulted]
        missing += texts("missing_information")
        checks = texts("checks")

        finding = answer.get("finding") if isinstance(answer.get("finding"), str) else ""
        finding = " ".join(finding.split())
        problem = text_problem(finding, allowed_numbers) if finding else None
        if problem:
            discarded.append(f"finding: {problem}")
            finding = ""
        if finding and not evidence:
            finding = ""  # a finding must rest on cited evidence

        references = [
            Evidence(kind=EvidenceKind.KNOWLEDGE, source=f"knowledge:{hit['source']}",
                     statement=f"{hit['doc_id']} ({hit['title']}): {hit['excerpt']}")
            for result in s.knowledge for hit in (result.data or [])[:2]
        ]
        if not references and causes:
            references, more_checks = gather_references(gateway, self.domain, [c.cause for c in causes])
            checks += more_checks
        open_causes = [c for c in causes if c.status is HypothesisStatus.HYPOTHESIS]
        return AgentDiagnosis(
            agent=self.name,
            domain=self.domain,
            finding=finding,
            evidence=evidence,
            possible_causes=causes,
            affected_components=dedupe(components),
            recommended_checks=dedupe(checks),
            missing_information=dedupe(missing),
            references=dedupe_evidence(references),
            confidence=max((c.confidence for c in open_causes), default=0.0),
            reasoning="llm",
            reasoning_note=f"{self._llm.name}:{self._llm.model}; {s.calls} tool call(s)",
            discarded_proposals=dedupe([safe_note(d) for d in discarded]),
        )


class _Unusable(ValueError):
    """The model's answer could not be used at all (the rules run instead)."""


def dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    out: list[Evidence] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


# --- triage --------------------------------------------------------------------------------

class LLMTriageAgent:
    """The model routes the incident; the keyword rules can only add domains, never remove one."""

    name = "triage-agent"

    def __init__(self, llm: ChatLLM, definition: AgentDefinition | None = None) -> None:
        self._llm = llm
        self._rules = TriageAgent()
        self._definition = definition or load_definitions()[self.name]

    def triage(self, incident: Incident) -> TriageResult:
        rules = self._rules.triage(incident)
        try:
            return self._triage(incident, rules)
        except (LLMUnavailableError, _Unusable) as error:
            return rules.model_copy(update={"reasoning": "rules-fallback",
                                            "reasoning_note": f"{self._llm.model}: {error}"})

    def _triage(self, incident: Incident, rules: TriageResult) -> TriageResult:
        reported, items = incident_items(incident, with_source=False)
        answer = ask_json(self._llm, self._definition.render(), items or "no reported items")
        if answer is None:
            raise _Unusable("answer was not valid JSON")
        signals: dict[Domain, list[str]] = {}
        for entry in answer.get("domains") or []:
            if not isinstance(entry, dict):
                continue
            try:
                domain = Domain(entry.get("domain"))
            except ValueError:
                continue
            if domain not in AGENT_FOR_DOMAIN:
                continue
            sources = []
            for ref in _strings(entry.get("items")):
                index = int(ref[1:]) if re.fullmatch(r"I\d+", ref) else 0
                if 1 <= index <= len(reported):
                    sources.append(reported[index - 1].source)
            if sources:
                signals.setdefault(domain, []).extend(sources)
        model_domains = list(signals)
        added = [d for d in rules.domain_signals if d not in signals]
        for domain in added:  # the rules may add a domain the model missed, never remove one
            signals[domain] = list(rules.domain_signals[domain])
        ranked = sorted(signals, key=lambda d: (d not in model_domains, -len(signals[d]), d.value))
        primary = answer.get("primary") if isinstance(answer.get("primary"), str) else None
        if not ranked:
            classification = Domain.UNKNOWN
        elif primary in {d.value for d in model_domains} and len(model_domains) == 1:
            classification = Domain(primary)
        elif len(model_domains) > 1:
            classification = Domain.MULTI_DOMAIN
        else:
            classification = ranked[0] if len(ranked) == 1 else Domain.MULTI_DOMAIN
        note = f"{self._llm.name}:{self._llm.model}"
        if added:
            note += f"; keyword rules added: {', '.join(d.value for d in added)}"
        return TriageResult(
            incident_id=incident.incident_id,
            classification=classification,
            related_domains=[d for d in ranked if d != classification],
            domain_signals={d: dedupe(signals[d]) for d in ranked},
            selected_agents=[AGENT_FOR_DOMAIN[d] for d in ranked],
            symptoms=list(incident.symptoms),
            available_evidence=reported,
            missing_evidence=TriageAgent._missing(incident, signals),
            reasoning="llm",
            reasoning_note=note,
        )


__all__ = ["LLMSpecialist", "LLMTriageAgent", "leaves", "site_thresholds", "text_problem"]
