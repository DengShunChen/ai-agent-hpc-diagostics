"""End-to-end human-in-the-loop lifecycles on the POC scenarios, including the CLI."""

import re

import pytest

from hpc_agent import main as cli
from hpc_agent.models import ApprovalStatus as S


def test_storage_001_revision_cycle(service, adapter):
    first = service.diagnose(adapter.load_incident("POC-STORAGE-001"))
    review = service.review(first.run_id)
    assert review.actions == [] and review.confidence == 0.25
    assert "individual OST utilization" in review.missing_information

    service.decide(first.run_id, S.REVISION_REQUESTED, "ops.lee", "collect per-OST utilization first")
    second = service.revise(first.run_id, "ops.lee", "re-run")
    service.decide(second.run_id, S.APPROVED, "ops.lee", "assessment accepted")
    service.close(second.run_id, "ops.lee")

    assert [r.status for r in service.history(first.run_id)][-2:] == [S.REVISION_REQUESTED, S.REVISE]
    assert service.status(second.run_id).status is S.CLOSED
    assert service.audit.verify() == 6 and service.approvals.verify() == 10
    assert all(a["run_id"] in (first.run_id, second.run_id) for a in service.audit.records())


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_AGENT_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_lifecycle(cli_env, capsys):
    code, out, _ = run_cli(capsys, "diagnose", "POC-SCHEDULER-001")
    assert code == 0 and "AWAITING_HUMAN_APPROVAL" in out
    run_id = re.findall(r"run-[0-9a-f]{12}", out)[-1]

    code, out, _ = run_cli(capsys, "review", run_id)
    assert code == 0 and "APPROVAL REVIEW" in out and "this platform executes nothing" in out

    code, _, err = run_cli(capsys, "approve", run_id, "--operator", "scheduler-agent")
    assert code == 2 and "human operator" in err

    code, out, _ = run_cli(capsys, "approve", run_id, "--operator", "ops.wang", "--comment", "ok")
    assert code == 0 and "Nothing has been executed" in out

    code, out, _ = run_cli(capsys, "close", run_id, "--operator", "ops.wang")
    assert code == 0 and "CLOSED" in out

    code, out, _ = run_cli(capsys, "history", run_id)
    assert out.count("\n") == 5 and "APPROVED by ops.wang - ok" in out

    code, out, _ = run_cli(capsys, "verify-audit")
    assert code == 0 and "chain OK" in out


def test_cli_no_save_records_nothing(cli_env, capsys):
    code, out, _ = run_cli(capsys, "diagnose", "POC-GPU-001", "--no-save")
    assert code == 0 and "Review with:" not in out
    assert not (cli_env / "audit.jsonl").exists()


def test_cli_requires_operator(cli_env, capsys):
    with pytest.raises(SystemExit):
        cli.main(["approve", "run-000000000000"])
