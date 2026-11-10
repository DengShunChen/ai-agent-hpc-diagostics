"""Runtime invariants checked on every diagnostic run before it is returned.

Model validation guards individual objects; these checks guard the run as a whole, so a
defect anywhere in the agents fails the run instead of reaching a human reviewer.
"""

from __future__ import annotations

from collections.abc import Mapping

from hpc_agent.models import DiagnosticRun, EvidenceKind


class SafetyViolation(RuntimeError):
    pass


def verify_run(run: DiagnosticRun, scopes: Mapping[str, frozenset[str]]) -> None:
    problems: list[str] = []

    if run.rca.confirmed_root_cause is not None:
        problems.append("root-cause confirmation is reserved for a human reviewer in this POC")

    for call in run.tools_invoked:
        if call.agent is None or call.tool not in scopes.get(call.agent, frozenset()):
            problems.append(f"tool call outside scope: {call.agent} -> {call.tool}")

    succeeded = {(c.tool, c.evidence_reference) for c in run.tools_invoked if c.result_status == "ok"}
    evidence = [*run.rca.facts, *(e for d in run.diagnoses for e in d.evidence)]
    for item in evidence:
        if item.kind is EvidenceKind.KNOWLEDGE:
            problems.append(f"knowledge presented as a fact: {item.source}")
        elif item.kind is EvidenceKind.TOOL_RESULT:
            _, tool, path = (item.source.split(":", 2) + ["", ""])[:3]
            if tool == "load_skill":
                problems.append(f"a skill presented as evidence: {item.source}")
            if (tool, path) not in succeeded:
                problems.append(f"tool evidence without a matching successful call: {item.source}")

    knowledge_calls = [c.evidence_reference for c in run.tools_invoked
                       if c.tool.startswith("search_") and c.result_status == "ok" and c.evidence_reference]
    for ref in [*run.rca.references, *(r for d in run.diagnoses for r in d.references)]:
        path = ref.source.removeprefix("knowledge:")
        if not any(path.startswith(f"{collection}/") for collection in knowledge_calls):
            problems.append(f"reference without a matching knowledge search: {ref.source}")

    for action in run.rca.recommended_actions:
        if not any(e.kind is EvidenceKind.TOOL_RESULT for e in action.supporting_evidence):
            problems.append(f"action without tool evidence: {action.action}")

    if problems:
        raise SafetyViolation("; ".join(problems))
