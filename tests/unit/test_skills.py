"""Skills: repository SKILL.md procedures, read on demand with load_skill; reference only."""

import pytest

from hpc_agent.agents.llm_agents import LLMSpecialist
from hpc_agent.agents.scheduler import SchedulerAgent
from hpc_agent.models import Domain, EvidenceKind
from hpc_agent.runtime.skills import SkillError, SkillLibrary, parse_skill
from hpc_agent.runtime.trace import TraceRecorder
from hpc_agent.safety.guards import SafetyViolation, verify_run
from hpc_agent.tools.gateway import ScopedGateway
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis
from tests.conftest import PROJECT_ROOT
from tests.safety.test_llm_agents import DRAIN, FakeLLM, answer, calls

SKILL = """---
name: demo-skill
description: "A demo"
agents: [scheduler-agent]
---
Do the thing.
"""


def test_repository_skills_load_and_have_a_version():
    library = SkillLibrary.load(PROJECT_ROOT)
    assert {s.name for s in library.for_agent("scheduler-agent")} == {"job-termination"}
    assert {s.name for s in library.for_agent("gpu-agent")} == {"xid-triage"}
    assert len(library.version()) == 12 and SkillLibrary({}).version() == "none"


@pytest.mark.parametrize("text", [
    "no frontmatter", SKILL.replace("name: demo-skill", "name: Bad Name"),
    SKILL.replace('description: "A demo"', 'description: ""'), SKILL.replace("agents: [scheduler-agent]", "agents: []"),
    SKILL.replace("agents: [scheduler-agent]", "agents: [scheduler-agent]\nrun: true"),
    SKILL.replace("Do the thing.", ""), SKILL.replace("Do the thing.", "x" * 9000),
])
def test_malformed_skills_are_refused(text):
    with pytest.raises(SkillError):
        parse_skill(text, "skills/x/demo-skill/SKILL.md")


def test_credential_like_skills_are_refused():
    from hpc_agent.safety.secrets import SensitiveDataError

    with pytest.raises(SensitiveDataError):
        parse_skill(SKILL.replace("Do the thing.", "login with password=hunter2"), "x")


def test_prompt_shows_only_the_index_and_skill_text_is_reference_only(adapter, gateway, scopes):
    trace = TraceRecorder()
    llm = FakeLLM({"scheduler": [
        {"tool_calls": [{"name": "load_skill", "arguments": {"skill": "job-termination"}}]},
        calls("get_job_status", "get_node_status"),
        answer(hypotheses=[{"cause": "Node drained", "category": "node.unhealthy", "confidence": 0.5,
                            "facts": [DRAIN, {"ref": "T3", "field": "body", "value": "x"}]},
                           {"cause": "Node drained", "category": "node.unhealthy", "confidence": 0.5,
                            "facts": [DRAIN]}])]})
    agent = LLMSpecialist(SchedulerAgent(), Domain.SCHEDULER, llm, emit=trace.for_agent("scheduler-agent"))
    d = agent.diagnose(adapter.load_incident("POC-SCHEDULER-001"), ScopedGateway(gateway, "scheduler-agent", scopes, []))
    system = llm.seen[0][2][0]["content"]
    assert "- job-termination: Reading a failed job" in system and "## Exit code" not in system
    assert d.skills_used and d.skills_used[0].startswith("job-termination@") and "skills: job-termination@" in d.reasoning_note
    tool_reply = next(m["content"] for m in llm.seen[1][2] if m["role"] == "tool")
    assert tool_reply.startswith("load_skill job-termination (reference procedure, not evidence")
    assert any("T3" in p for p in d.discarded_proposals)  # skill text can't be cited as a fact
    assert all(e.kind is not EvidenceKind.TOOL_RESULT or "load_skill" not in e.source for e in d.evidence)


def test_unknown_or_foreign_skills_are_refused(adapter, gateway, scopes):
    log = []
    llm = FakeLLM({"scheduler": [
        {"tool_calls": [{"name": "load_skill", "arguments": {"skill": "xid-triage"}},
                        {"name": "load_skill", "arguments": {"skill": "../../etc/passwd"}}]},
        answer(hypotheses=[])]})
    LLMSpecialist(SchedulerAgent(), Domain.SCHEDULER, llm).diagnose(
        adapter.load_incident("POC-SCHEDULER-001"), ScopedGateway(gateway, "scheduler-agent", scopes, log))
    replies = [m["content"] for m in llm.seen[-1][2] if m["role"] == "tool"]
    assert replies == ["error: unknown skill; use a name from your skills list"] * 2
    assert not any(c.tool == "load_skill" for c in log)


def test_skills_are_not_listed_as_data_tools(adapter, gateway, scopes):
    llm = FakeLLM({"scheduler": [answer(hypotheses=[])]})
    d = LLMSpecialist(SchedulerAgent(), Domain.SCHEDULER, llm).diagnose(
        adapter.load_incident("POC-SCHEDULER-001"), ScopedGateway(gateway, "scheduler-agent", scopes, []))
    assert "Start by calling your data tools (analyze_logs, get_job_status, get_log_digest, get_log_lines, get_node_status)" in llm.seen[0][2][0]["content"]
    assert not any("load_skill" in m for m in d.missing_information)


def test_verify_run_rejects_a_skill_presented_as_evidence(adapter, gateway, scopes):
    from hpc_agent.models import Evidence

    run = run_diagnosis(adapter.load_incident("POC-SCHEDULER-001"), gateway, scopes)
    fake = Evidence(kind=EvidenceKind.TOOL_RESULT, source="tool:load_skill:skills/x/y/SKILL.md", statement="x")
    diagnosis = run.diagnoses[0].model_copy(update={"evidence": [*run.diagnoses[0].evidence, fake]})
    with pytest.raises(SafetyViolation, match="skill presented as evidence"):
        verify_run(run.model_copy(update={"diagnoses": [diagnosis, *run.diagnoses[1:]]}), scopes)


def test_llm_evaluation_records_the_skills_version(tmp_path):
    from hpc_agent.evaluation.harness import evaluate
    from hpc_agent.evaluation.report import render_markdown

    (tmp_path / "cases").mkdir()
    source = PROJECT_ROOT / "evaluation" / "cases" / "POC-SCHEDULER-001.json"
    (tmp_path / "cases" / source.name).write_text(source.read_text())
    results, summary = evaluate(tmp_path / "cases", llm=FakeLLM({"scheduler": [answer(hypotheses=[])]}))
    assert summary.skills_version == SkillLibrary.load(PROJECT_ROOT).version()
    report = render_markdown(results, summary)
    assert f"`{summary.skills_version}`" in report and "**2026-11-10: skills.**" in report
