"""analyze_logs: fixed analyses over the saved log index; the model picks an operation and plain keywords."""

import pytest

from hpc_agent.agents.gpu import GpuAgent
from hpc_agent.agents.llm_agents import LLMSpecialist
from hpc_agent.models import Domain
from hpc_agent.safety.guards import verify_run
from hpc_agent.tools.gateway import ScopedGateway, ToolArgumentError, build_default_gateway
from tests.conftest import make_data_root
from tests.safety.test_llm_agents import FakeLLM, answer, calls

A, B = "sim-node-0001", "sim-node-0002"
DOWN = "kernel: mlx5_core 0000:5e:00.0: mlx5_port_event: port 1 link down"
LNET = "kernel: LNetError: 3511:0:(o2iblnd_cb.c:3402:kiblnd_check_txs_locked()) Timed out tx: tx_queue, 6 seconds"


def line(n, node, minute, second, text):
    return {"line": n, "node": node, "timestamp": f"2026-03-02T04:{minute:02d}:{second:02d}+00:00", "text": text}


SYSLOG = [line(2, A, 0, 0, DOWN), line(3, A, 1, 30, DOWN), line(4, B, 2, 0, "sshd[1]: session opened"),
          line(5, A, 2, 10, LNET), line(6, B, 40, 0, LNET), line(7, A, 41, 0, "chronyd[2]: Selected source")]
PJM = [{"line": 2, "timestamp": "2026-03-02T04:45:00+00:00", "text": "JOB 7 ENDED: end_code=5 signal=0"}]
INCIDENT = {"incident_id": "SIM-ANA-001", "severity": "medium", "job_id": "7", "symptoms": ["job failed"]}


@pytest.fixture
def root(tmp_path):
    return make_data_root(tmp_path, INCIDENT, {"logs/syslog": {"source": "syslog", "lines": SYSLOG, "unparsed": 0},
                                               "logs/pjm": {"source": "pjm", "lines": PJM, "unparsed": 0}})


def analyze(root, operation, keywords, also="x", window="300"):
    return build_default_gateway(root).invoke("analyze_logs", incident_id="SIM-ANA-001", operation=operation,
                                              keywords=keywords, also_keywords=also, window_seconds=window).data


def test_count_by_node_groups_matches_and_keeps_job_manager_lines_nodeless(root):
    rows = analyze(root, "count_by_node", "kernel")["rows"]
    assert rows[0] == {"match": "kernel", "log": "syslog", "node": A, "count": 3, "first_seen": SYSLOG[0]["timestamp"],
                       "first_line": 2, "last_seen": SYSLOG[3]["timestamp"]}
    [job] = analyze(root, "count_by_node", "ended")["rows"]
    assert job["log"] == "pjm" and "node" not in job


def test_timeline_counts_per_window(root):
    rows = analyze(root, "timeline", "lneterror", window="600")["rows"]
    assert [(r["bucket"][11:16], r["count"]) for r in rows] == [("04:00", 1), ("04:40", 1)]


def test_near_finds_lines_shortly_after_an_anchor(root):
    data = analyze(root, "near", "link down", also="lneterror", window="120")
    [row] = data["rows"]  # B's LNet error 38 minutes later is outside the window
    assert row["line"] == 5 and row["node"] == A and row["text"] == LNET and row["same_node"]
    assert row["seconds_after_anchor"] == 40.0 and row["anchor_line"] == 3


def test_nodes_with_both(root):
    [row] = analyze(root, "nodes_with_both", "link down", also="lneterror")["rows"]
    assert row["node"] == A and row["count_keywords"] == 2 and row["count_also"] == 1


def test_no_logs_is_no_data(tmp_path):
    empty = make_data_root(tmp_path, INCIDENT)
    assert build_default_gateway(empty).invoke("analyze_logs", incident_id="SIM-ANA-001", operation="timeline",
                                               keywords="x", also_keywords="x", window_seconds="60").status == "no_data"


def test_window_is_clamped(root):
    assert analyze(root, "near", "link", also="lnet", window="9999")["window_seconds"] == 3600


@pytest.mark.parametrize("kwargs", [
    {"operation": "exec"}, {"operation": "near;rm"}, {"operation": "count_by_node "},
    {"keywords": "link|down"}, {"keywords": ".*"}, {"keywords": "../../etc/passwd"},
    {"also_keywords": "$(reboot)"}, {"window_seconds": "0"}, {"window_seconds": "-5"}, {"window_seconds": "1e3"},
    {"window_seconds": "99999"},
])
def test_gateway_rejects_bad_analysis_arguments(root, kwargs):
    args = {"incident_id": "SIM-ANA-001", "operation": "near", "keywords": "link", "also_keywords": "lnet",
            "window_seconds": "60"} | kwargs
    with pytest.raises(ToolArgumentError):
        build_default_gateway(root).invoke("analyze_logs", **args)


def scoped(scopes):
    return {**scopes, "gpu-agent": scopes["gpu-agent"] | {"analyze_logs"}}


def test_harness_refuses_incomplete_analyses(root, scopes):
    llm = FakeLLM({"gpu": [calls("analyze_logs", operation="near", keywords="link down"), answer(hypotheses=[])]})
    log = []
    LLMSpecialist(GpuAgent(), Domain.GPU, llm).diagnose(
        root.load_incident("SIM-ANA-001"), ScopedGateway(build_default_gateway(root), "gpu-agent", scoped(scopes), log))
    assert log == []  # near needs also_keywords: refused before the gateway


def test_analysis_rows_become_checked_facts(root, scopes):
    from hpc_agent.workflows.diagnostic_workflow import run_diagnosis

    llm = FakeLLM({
        "triage": [answer(domains=[{"domain": "gpu", "items": ["I1"]}], primary="gpu")],
        "gpu": [calls("analyze_logs", operation="near", keywords="link down", also_keywords="lneterror",
                      window_seconds=120),
                answer(hypotheses=[{"cause": "Link loss preceded the LNet timeout", "category": "gpu.interconnect",
                                    "confidence": 0.4, "facts": [
                                        {"ref": "T1", "field": "text", "value": LNET, "where": "5"},
                                        {"ref": "T1", "field": "seconds_after_anchor", "value": 40.0, "where": "5"}]}])],
    })
    run = run_diagnosis(root.load_incident("SIM-ANA-001"), build_default_gateway(root), scoped(scopes), llm=llm)
    verify_run(run, scopes=scoped(scopes))
    [cause] = [c for c in run.rca.probable_causes if c.category == "gpu.interconnect"]
    statements = [e.statement for e in cause.supporting_evidence]
    assert any(s.startswith(f"log=syslog, node={A}, line=5: text=") for s in statements)
    assert any("seconds_after_anchor=40.0" in s for s in statements)


def test_analysis_quote_counts_as_a_cited_log_line(root, scopes):
    from types import SimpleNamespace

    from hpc_agent.evaluation.scenarios import SIMULATION_KEY, _cited
    from hpc_agent.ingest.deidentify import Pseudonymizer

    evidence = SimpleNamespace(statement=f'log=syslog, node={A}, line=5: text="{LNET}"',
                               source="tool:analyze_logs:test_data/logs/SIM-ANA-001")
    run = SimpleNamespace(diagnoses=[SimpleNamespace(evidence=[evidence])],
                          rca=SimpleNamespace(facts=[], probable_causes=[]))
    assert _cited(run, Pseudonymizer(SIMULATION_KEY)) == {("syslog", A, 5)}
