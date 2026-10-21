import pytest

from hpc_agent import metrics
from hpc_agent.models import ApprovalStatus as S


def test_no_runs(service):
    assert metrics.summarize(metrics.collect(service)) == []
    assert "No recorded runs" in metrics.render([])


def test_counts_timings_and_override_rate(service, adapter):
    ids = [service.diagnose(adapter.load_incident(i)).run_id
           for i in ("POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001")]
    service.decide(ids[0], S.REVISION_REQUESTED, "ops.lee", "need OST data")
    service.decide(ids[1], S.APPROVED, "ops.lee")
    service.close(ids[1], "ops.lee")
    timings = {t.run_id: t for t in metrics.collect(service)}
    [site] = metrics.summarize(list(timings.values()))
    assert (site.site, site.runs, site.decided, site.awaiting_decision) == ("default", 3, 2, 1)
    assert (site.approved, site.revision_requested, site.human_override_rate) == (1, 1, 0.5)
    assert timings[ids[1]].review_s >= 0 and timings[ids[1]].decision_to_close_s >= 0
    assert timings[ids[2]].review_s is None and timings[ids[2]].decision is None  # never simulated
    assert timings[ids[1]].report_to_diagnosis_s > 0  # fixture has an incident timestamp
    assert timings[ids[0]].report_to_diagnosis_s is None  # POC-STORAGE-001 has none
    text = metrics.render([site])
    assert "not MTTR" in text and "50%" in text


def test_grouped_by_site(service, adapter, monkeypatch):
    service.diagnose(adapter.load_incident("POC-GPU-001"))
    monkeypatch.setenv("HPC_AGENT_SITE", "example-pjm-site")
    service.diagnose(adapter.load_incident("POC-GPU-001"))
    sites = {m.site: m.runs for m in metrics.summarize(metrics.collect(service))}
    assert sites == {"default": 1, "example-pjm-site": 1}


def test_metrics_api(api, service, adapter):
    assert api.get("/api/metrics").status_code == 401
    service.diagnose(adapter.load_incident("POC-GPU-001"))
    body = api.get("/api/metrics", headers=api.auth_header()).json()
    assert "not MTTR" in body["note"] and body["sites"][0]["runs"] == 1


@pytest.mark.parametrize("seconds,text", [(0.004, "4 ms"), (12.5, "12.5 s"), (600, "10.0 min"), (10800, "3.0 h")])
def test_formatting(seconds, text):
    assert metrics._fmt(seconds) == text
