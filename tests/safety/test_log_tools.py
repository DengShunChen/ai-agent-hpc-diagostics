"""get_log_lines: a fixed set of sources, plain keywords, saved data only; log facts must quote lines exactly."""

import pytest

from hpc_agent.agents.llm_agents import LLMSpecialist
from hpc_agent.agents.gpu import GpuAgent
from hpc_agent.models import Domain
from hpc_agent.safety.guards import verify_run
from hpc_agent.tools.gateway import ScopedGateway, ToolArgumentError, build_default_gateway
from hpc_agent.tools.log_tools import MAX_SHOWN_CHARS
from tests.conftest import make_data_root
from tests.safety.test_llm_agents import FakeLLM, answer, calls

XID = "kernel: NVRM: Xid (PCI:0000:3b:00): 79, pid=4242, name=python3, GPU has fallen off the bus."
LINES = [{"line": n, "node": "sim-gpu-node-0001", "timestamp": "2026-03-02T04:00:00+00:00", "text": f"routine {n}"}
         for n in range(2, 400)] + [{"line": 400, "node": "sim-gpu-node-0001",
                                     "timestamp": "2026-03-02T04:17:36+00:00", "text": XID}]
INCIDENT = {"incident_id": "SIM-LOG-001", "severity": "medium", "job_id": "7000001",
            "symptoms": ["training job crashed with a CUDA error"]}


@pytest.fixture
def log_root(tmp_path):
    return make_data_root(tmp_path, INCIDENT, {"logs/syslog": {"source": "syslog", "lines": LINES, "unparsed": 0}})


def test_search_returns_only_matching_lines(log_root):
    result = build_default_gateway(log_root).invoke("get_log_lines", incident_id="SIM-LOG-001", source="syslog",
                                                    keywords="Xid fallen")
    assert result.data["total_matches"] == 1 and result.data["lines"][0]["line"] == 400


def test_search_output_is_capped_to_whole_lines(log_root):
    result = build_default_gateway(log_root).invoke("get_log_lines", incident_id="SIM-LOG-001", source="syslog",
                                                    keywords="routine")
    assert result.data["total_matches"] == 398 and 0 < result.data["shown"] < 398
    assert len(str(result.data["lines"])) <= MAX_SHOWN_CHARS + 500


def test_missing_source_is_no_data(log_root):
    assert build_default_gateway(log_root).invoke("get_log_lines", incident_id="SIM-LOG-001", source="slurmd",
                                                  keywords="x").status == "no_data"


@pytest.mark.parametrize("kwargs", [
    {"source": "messages", "keywords": "Xid"},
    {"source": "../syslog", "keywords": "Xid"},
    {"source": "syslog", "keywords": "Xid|reboot"},
    {"source": "syslog", "keywords": ".*"},
    {"source": "syslog", "keywords": "../../etc/passwd"},
    {"source": "syslog", "keywords": "x" * 121},
    {"source": "syslog", "keywords": "a; rm -rf /"},
    {"source": "syslog"},
])
def test_gateway_rejects_bad_log_arguments(log_root, kwargs):
    with pytest.raises(ToolArgumentError):
        build_default_gateway(log_root).invoke("get_log_lines", incident_id="SIM-LOG-001", **kwargs)


def gpu_agent(llm):
    return LLMSpecialist(GpuAgent(), Domain.GPU, llm)


def run_gpu(log_root, scopes, fact):
    llm = FakeLLM({"gpu": [calls("get_log_lines", source="syslog", keywords="Xid"), answer(
        finding="The GPU fell off the bus.",
        hypotheses=[{"cause": "GPU hardware failure", "category": "gpu.hardware", "confidence": 0.5, "facts": [fact]}])]})
    log = []
    diagnosis = gpu_agent(llm).diagnose(log_root.load_incident("SIM-LOG-001"),
                                        ScopedGateway(build_default_gateway(log_root), "gpu-agent", scopes, log))
    return diagnosis, log


def test_an_exactly_quoted_log_line_is_kept(log_root, scopes):
    d, _ = run_gpu(log_root, scopes, {"ref": "T1", "field": "text", "value": XID, "where": "400"})
    [cause] = d.hypotheses
    [evidence] = cause.supporting_evidence
    assert evidence.source == "tool:get_log_lines:test_data/logs/SIM-LOG-001/syslog.json"
    assert "line=400" in evidence.statement and "fallen off the bus" in evidence.statement


@pytest.mark.parametrize("value", ["GPU has fallen off the bus", XID.replace("79", "48"), "Xid 79 on GPU"])
def test_a_paraphrased_log_line_is_discarded(log_root, scopes, value):
    d, _ = run_gpu(log_root, scopes, {"ref": "T1", "field": "text", "value": value, "where": "400"})
    assert d.hypotheses == [] and any("has no text=" in note for note in d.discarded_proposals)


def test_log_search_needs_a_listed_source(log_root, scopes):
    llm = FakeLLM({"gpu": [calls("get_log_lines", source="/var/log/messages", keywords="Xid"), answer(hypotheses=[])]})
    log = []
    gpu_agent(llm).diagnose(log_root.load_incident("SIM-LOG-001"),
                            ScopedGateway(build_default_gateway(log_root), "gpu-agent", scopes, log))
    assert log == []  # refused by the harness before reaching the gateway


def test_runs_with_log_evidence_pass_verify_run(log_root, scopes):
    from hpc_agent.workflows.diagnostic_workflow import run_diagnosis

    llm = FakeLLM({
        "triage": [answer(domains=[{"domain": "gpu", "items": ["I1"]}], primary="gpu")],
        "gpu": [calls("get_log_lines", source="syslog", keywords="Xid"), answer(
            hypotheses=[{"cause": "GPU hardware failure", "category": "gpu.hardware", "confidence": 0.5,
                         "facts": [{"ref": "T1", "field": "text", "value": XID, "where": "400"}]}])]})
    run = run_diagnosis(log_root.load_incident("SIM-LOG-001"), build_default_gateway(log_root), scopes, llm=llm)
    verify_run(run, scopes)
    assert any("line=400" in e.statement for c in run.rca.probable_causes for e in c.supporting_evidence)
