import json

import pytest

from hpc_agent import main as cli
from hpc_agent.evaluation.harness import evaluate, human_override, load_cases
from hpc_agent.evaluation.report import render_markdown
from hpc_agent.logging.audit_logger import AppendOnlyLog
from tests.conftest import PROJECT_ROOT

CASES = PROJECT_ROOT / "evaluation" / "cases"


@pytest.fixture(scope="module")
def evaluation():
    return evaluate(CASES)


def test_cases_are_valid_and_unique():
    cases = load_cases(CASES)
    assert len(cases) >= 13
    assert len({c.case_id for c in cases}) == len(cases)
    for case in cases:
        assert "synthetic" in case.label_source
        assert (case.fixture or case.incident["incident_id"]) == case.case_id


def test_no_unsupported_claims_on_any_case(evaluation):
    results, summary = evaluation
    assert [(r.case_id, r.unsupported_claims) for r in results if r.unsupported_claims] == []
    assert summary.unsupported_claim_rate == 0.0


def test_refusals_are_correct(evaluation):
    results, summary = evaluation
    refusal_cases = [r for r in results if r.expected_outcome != "supported_hypothesis"]
    assert {r.case_id for r in refusal_cases} >= {"POC-STORAGE-001", "EVAL-STOR-002", "EVAL-UNK-001"}
    assert summary.refusal_accuracy == 1.0


@pytest.mark.parametrize("case_id", ["POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001"])
def test_poc_scenarios_fully_correct(evaluation, case_id):
    result = next(r for r in evaluation[0] if r.case_id == case_id)
    assert result.classification_correct and result.outcome_correct and result.leading_correct
    assert result.missing_found == result.missing_required


def test_summary_is_consistent(evaluation):
    results, summary = evaluation
    assert summary.cases == len(results)
    assert summary.human_override_rate is None and summary.human_decisions == 0
    assert 0 < summary.processing_ms_median <= summary.processing_ms_max


def test_forbidden_conclusion_is_counted(tmp_path):
    case = json.loads((CASES / "POC-STORAGE-001.json").read_text())
    case["expected"]["forbidden_categories"] = ["storage.capacity"]
    (tmp_path / "case.json").write_text(json.dumps(case))
    results, summary = evaluate(tmp_path)
    assert results[0].unsupported_claims and summary.unsupported_claims == 1


def test_label_mismatch_is_reported(tmp_path):
    case = json.loads((CASES / "POC-GPU-001.json").read_text())
    case["expected"] |= {"classification": ["storage"], "outcome": "no_supported_hypothesis"}
    (tmp_path / "case.json").write_text(json.dumps(case))
    results, summary = evaluate(tmp_path)
    assert not results[0].classification_correct and not results[0].outcome_correct
    assert "## Mismatches against labels\n\n- POC-GPU-001" in render_markdown(results, summary)


def test_override_rate_only_from_recorded_decisions(tmp_path):
    assert human_override(None) == (None, 0)
    log = AppendOnlyLog(tmp_path / "approvals.jsonl")
    for status in ["DRAFT", "AWAITING_HUMAN_APPROVAL", "APPROVED", "REJECTED", "REVISION_REQUESTED", "CLOSED"]:
        log.append({"status": status})
    rate, decisions = human_override(log.path)
    assert decisions == 3 and rate == pytest.approx(2 / 3)


def test_report_states_limits(evaluation):
    report = render_markdown(*evaluation)
    assert "No MTTR reduction is claimed" in report
    assert "not evidence of accuracy on real incidents" in report.replace("**", "")
    assert "not measured: no recorded human decisions" in report


def test_cli_writes_report_and_json(tmp_path, capsys):
    assert cli.main(["evaluate", "--output", str(tmp_path / "r.md"), "--json", str(tmp_path / "r.json")]) == 0
    capsys.readouterr()
    assert (tmp_path / "r.md").read_text().startswith("# POC Evaluation Report")
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["summary"]["cases"] == len(data["cases"])
