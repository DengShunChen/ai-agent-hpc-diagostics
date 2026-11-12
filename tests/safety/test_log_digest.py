"""get_log_digest: the warning and error lines on the failed job's own nodes while it ran; fixed terms, no model."""

from hpc_agent.models import HypothesisStatus
from hpc_agent.safety.guards import verify_run
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis
from tests.conftest import make_data_root

A, B, C = "sim-node-0001", "sim-node-0002", "sim-node-0003"
LINK = "kernel: mlx5_core 0000:5e:00.0: mlx5_port_event: port 1 link down"
LUSTRE = "kernel: LustreError: 11-0: fs-01-OST0003-osc: operation ost_write to node sim-oss-03@o2ib failed: rc = -110"


def line(n, node, hour, minute, text):
    return {"line": n, "node": node, "timestamp": f"2026-03-02T{hour:02d}:{minute:02d}:00+00:00", "text": text}


SYSLOG = [
    line(2, A, 1, 0, "kernel: EDAC MC0: 1 CE memory read error"),  # before the job started
    line(3, A, 3, 0, LINK),
    line(4, A, 3, 1, LINK.replace("down", "up")),
    line(5, A, 3, 2, LINK),  # the same message again: folded into line 3
    line(6, A, 3, 5, LUSTRE),
    line(7, B, 3, 6, "sshd[1]: session opened for user sim-user"),  # job node, no error term
    line(8, C, 3, 7, LUSTRE),  # another node in the same window: counted, not listed
    line(9, A, 5, 0, LUSTRE.replace("OST0003", "OST0004")),  # after the window
]
SLURMCTLD = [line(2, None, 4, 0, "_job_complete: JobId=job-77 WEXITSTATUS 5 error"),
             line(3, None, 4, 0, "_job_complete: JobId=job-99 WEXITSTATUS 1 error")]
JOB = {"job_id": "job-77", "scheduler": "slurm", "state": "FAILED", "exit_code": 5, "requested_walltime": "12:00:00",
       "elapsed": "02:00:00", "end_time": "2026-03-02T04:00:00Z", "allocated_nodes": [A, B]}
INCIDENT = {"incident_id": "SIM-DIG-001", "severity": "medium", "job_id": "job-77", "symptoms": ["job failed"]}


def root(tmp_path, job=JOB):
    return make_data_root(tmp_path, INCIDENT, {
        "scheduler/job_status": job,
        "logs/syslog": {"source": "syslog", "lines": SYSLOG, "unparsed": 0},
        "logs/slurmctld": {"source": "slurmctld", "lines": SLURMCTLD, "unparsed": 0}})


def digest(adapter):
    return build_default_gateway(adapter).invoke("get_log_digest", incident_id="SIM-DIG-001")


def test_only_error_lines_on_the_jobs_nodes_while_it_ran_oldest_first_repeats_folded(tmp_path):
    data = digest(root(tmp_path)).data
    assert [(r.get("node"), r["line"]) for r in data["rows"]] == [(A, 3), (A, 6), (None, 2)]  # "link up" has no error term
    assert data["rows"][0]["repeats"] == 2 and data["rows"][0]["last_seen"] == "2026-03-02T03:02:00+00:00"
    assert data["elsewhere"] == {"lines": 2, "nodes": 1}  # node C, and job-99 in slurmctld
    assert data["window_start"] == "2026-03-02T02:00:00+00:00" and data["window_end"] == "2026-03-02T04:05:00+00:00"


def test_no_job_data_or_no_nodes_gives_no_data(tmp_path):
    assert digest(make_data_root(tmp_path / "a", INCIDENT)).status.value == "no_data"
    assert digest(root(tmp_path / "b", {**JOB, "allocated_nodes": []})).status.value == "no_data"


def test_the_rules_quote_the_lines_exactly_and_make_no_hypothesis_from_them(tmp_path, scopes):
    adapter = root(tmp_path)
    run = run_diagnosis(adapter.load_incident("SIM-DIG-001"), build_default_gateway(adapter), scopes)
    verify_run(run, scopes)  # every quoted line traces to the digest call in this run
    facts = [e.statement for d in run.diagnoses for e in d.evidence if "get_log_digest" in e.source]
    assert facts[0] == f"log=syslog, node={A}, line=3: text=\"{LINK}\" (logged 2 times until 2026-03-02T03:02:00+00:00)"
    assert all(any(r["text"] in f for f in facts) for r in digest(adapter).data["rows"])
    causes = [c for c in run.rca.probable_causes if c.status is HypothesisStatus.HYPOTHESIS]
    assert not any("get_log_digest" in e.source for c in causes for e in c.supporting_evidence)


def test_when_there_are_too_many_the_earliest_and_the_latest_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("hpc_agent.tools.log_tools.MAX_DIGEST_ROWS", 4)
    many = [line(n, A, 2, n, f"kernel: device{chr(96 + n)} error") for n in range(1, 11)]  # 10 distinct messages
    adapter = make_data_root(tmp_path, INCIDENT, {"scheduler/job_status": JOB,
                                                  "logs/syslog": {"source": "syslog", "lines": many, "unparsed": 0}})
    data = digest(adapter).data
    assert [r["line"] for r in data["rows"]] == [1, 2, 9, 10] and data["omitted"] == 6


def test_every_catalog_fault_can_be_answered_in_a_drill(tmp_path):
    from hpc_agent.evaluation.drill import answer_choices
    from hpc_agent.simulate.render import generate
    from hpc_agent.simulate.scenario import ALL_FAULTS, stratified_scenarios

    leading = {c for sc in stratified_scenarios(7, 1, ALL_FAULTS) for c in generate(sc, tmp_path).leading_categories}
    assert leading and leading <= set(answer_choices())
