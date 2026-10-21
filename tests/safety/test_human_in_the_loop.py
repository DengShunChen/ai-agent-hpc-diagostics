"""Human approval cannot be bypassed, impersonated by an agent, or turned into execution."""

import re

import pytest
from pydantic import ValidationError

from hpc_agent.models import ApprovalStatus as S
from hpc_agent.workflows import approval_workflow
from tests.conftest import PROJECT_ROOT

AI_IDENTITIES = ["triage-agent", "storage-agent", "rca-coordinator", "gpu_agent", "system", "AI", "llm",
                 "claude", "gpt", "approval-bot", "automation", "assistant"]


@pytest.fixture
def awaiting(service, adapter):
    return service.diagnose(adapter.load_incident("POC-GPU-001")).run_id


@pytest.mark.parametrize("operator", AI_IDENTITIES + ["", "x", "ops wang", "ops;rm"])
@pytest.mark.parametrize("decision", [S.APPROVED, S.REJECTED, S.REVISION_REQUESTED])
def test_ai_or_invalid_operator_cannot_decide(service, awaiting, operator, decision):
    before = (service.approvals.verify(), service.audit.verify())
    with pytest.raises((ValueError, ValidationError)):
        service.decide(awaiting, decision, operator, "comment")
    assert (service.approvals.verify(), service.audit.verify()) == before
    assert service.status(awaiting).status is S.AWAITING_HUMAN_APPROVAL


@pytest.mark.parametrize("operator", ["storage-agent", "rca-coordinator", "system"])
def test_ai_cannot_close_or_revise(service, awaiting, operator):
    service.decide(awaiting, S.APPROVED, "ops.wang")
    with pytest.raises(ValueError):
        service.close(awaiting, operator)


def test_credentials_in_comment_rejected_and_nothing_written(service, awaiting):
    before = (service.approvals.verify(), service.audit.verify())
    with pytest.raises((ValueError, ValidationError)):
        service.decide(awaiting, S.REJECTED, "ops.wang", "retry with password=hunter2-not-real")
    assert (service.approvals.verify(), service.audit.verify()) == before
    assert "hunter2" not in service.approvals.path.read_text() + service.audit.path.read_text()


def test_decisions_make_no_tool_calls(service, awaiting, monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("a decision must not invoke any tool")

    monkeypatch.setattr(service._gateway, "invoke", forbidden)
    service.decide(awaiting, S.APPROVED, "ops.wang", "ok")
    service.close(awaiting, "ops.wang")


def test_service_has_no_execution_surface():
    names = {n for n in dir(approval_workflow.ApprovalService) if not n.startswith("__")}
    assert not any(re.search(r"exec|run_|apply|remediat|shell|ssh|command", n) for n in names), names


def test_agents_and_diagnosis_cannot_reach_approval():
    sources = [*(PROJECT_ROOT / "src" / "hpc_agent" / "agents").rglob("*.py"),
               PROJECT_ROOT / "src" / "hpc_agent" / "workflows" / "diagnostic_workflow.py",
               *(PROJECT_ROOT / "src" / "hpc_agent" / "tools").rglob("*.py")]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "approval_workflow" not in text and "safety.approval" not in text, path.name
        assert "ApprovalService" not in text and "transition(" not in text, path.name


def test_only_human_interface_uses_approval_service():
    users = [p.relative_to(PROJECT_ROOT).as_posix() for p in (PROJECT_ROOT / "src").rglob("*.py")
             if "ApprovalService" in p.read_text(encoding="utf-8")]
    # Human-facing interfaces only: the operator CLI and the authenticated HTTP API.
    assert sorted(users) == ["src/hpc_agent/api/routes.py", "src/hpc_agent/main.py",
                             "src/hpc_agent/workflows/approval_workflow.py"]
