import pytest
from pydantic import ValidationError

from hpc_agent.config import ConfigError, load_site_profile
from hpc_agent.models.site import SiteProfile
from hpc_agent.tools.gateway import build_default_gateway
from hpc_agent.workflows.diagnostic_workflow import run_diagnosis
from tests.conftest import PROJECT_ROOT, make_data_root

EXAMPLE = "example-pjm-site"


def test_repo_profiles_load():
    default = load_site_profile("default", PROJECT_ROOT)
    example = load_site_profile(EXAMPLE, PROJECT_ROOT)
    assert default == SiteProfile(site_id="default", description=default.description)
    assert (example.storage_full_pct, example.gpu_thermal_limit_c, example.scheduler) == (90, 80, "pjm")


def test_site_selected_from_environment(monkeypatch):
    monkeypatch.setenv("HPC_AGENT_SITE", EXAMPLE)
    assert load_site_profile(root=PROJECT_ROOT).site_id == EXAMPLE


@pytest.mark.parametrize("site", ["../default", "Default", "", "x" * 40])
def test_invalid_site_ids(site, monkeypatch):
    monkeypatch.delenv("HPC_AGENT_SITE", raising=False)
    with pytest.raises(ConfigError):
        load_site_profile(site or "-bad", PROJECT_ROOT)


def write_profile(tmp_path, name, text):
    (tmp_path / "config" / "sites").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "sites" / f"{name}.yaml").write_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "site_id: lab\nallow_shell: true\n",            # a profile cannot add capabilities
        "site_id: lab\nautonomous_remediation: true\n",
        "site_id: lab\ntool_call_budget: 100000\n",      # budget is hard-capped
        "site_id: lab\nstorage_full_pct: 10\n",          # out of sane range
        "site_id: other\n",                              # must match its file name
        "[]",
    ],
)
def test_profiles_cannot_weaken_safety_or_be_malformed(tmp_path, text):
    write_profile(tmp_path, "lab", text)
    with pytest.raises(ConfigError):
        load_site_profile("lab", tmp_path)


def test_missing_profile(tmp_path):
    with pytest.raises(ConfigError, match="no site profile"):
        load_site_profile("nowhere", tmp_path)


def test_profile_fields_are_bounded():
    with pytest.raises(ValidationError):
        SiteProfile(site_id="lab", correlation_window_minutes=0)


# --- thresholds really change the diagnosis --------------------------------------------
def _run(tmp_path, incident, data, site):
    adapter = make_data_root(tmp_path, incident, data)
    profile = load_site_profile(site, PROJECT_ROOT)
    return run_diagnosis(adapter.load_incident(incident["incident_id"]), build_default_gateway(adapter),
                         profile=profile)


def _categories(run):
    return {c.category for c in run.rca.probable_causes if c.status == "hypothesis"}


def test_storage_threshold_per_site(tmp_path):
    incident = {"incident_id": "SIM-SITE-001", "severity": "high", "symptoms": ["job failed while writing output"]}
    data = {"storage/filesystem_usage": {"filesystem": "simfs01", "used_pct": 80, "health": "healthy"},
            "storage/ost_usage": {"osts": [{"ost": "OST0000", "used_pct": 88}, {"ost": "OST0001", "used_pct": 92}]}}
    default = _run(tmp_path / "a", incident, data, "default")
    example = _run(tmp_path / "b", incident, data, EXAMPLE)
    assert "storage.capacity" not in _categories(default)   # 92% < 95%
    assert "storage.capacity" in _categories(example)       # 92% >= 90%
    assert (default.site, example.site) == ("default", EXAMPLE)


def test_gpu_thermal_threshold_per_site(tmp_path):
    incident = {"incident_id": "SIM-SITE-002", "severity": "medium", "node": "sim-gpu-node-0001",
                "symptoms": ["GPU throughput dropped"]}
    data = {"gpu/gpu_health": {"node": "sim-gpu-node-0001", "gpus": [
        {"index": 0, "health": "warn", "xid_events": [], "temperature_c": 82, "power_w": 250,
         "ecc_dbe_volatile": 0, "pcie_replay_count": 0, "nvlink_errors": 0}]}}
    assert "gpu.thermal" not in _categories(_run(tmp_path / "a", incident, data, "default"))
    assert "gpu.thermal" in _categories(_run(tmp_path / "b", incident, data, EXAMPLE))


def test_correlation_window_per_site(tmp_path):
    incident = {"incident_id": "SIM-SITE-003", "severity": "medium", "timestamp": "2026-03-10T08:00:00Z",
                "job_id": "SIM-JOB-1", "symptoms": ["job terminated unexpectedly"]}
    data = {"scheduler/job_status": {"job_id": "SIM-JOB-1", "state": "FAILED", "exit_code": 137,
                                     "requested_walltime": "04:00:00", "elapsed": "01:00:00",
                                     "end_time": "2026-03-10T08:25:00Z", "allocated_nodes": []}}
    default = " ".join(_run(tmp_path / "a", incident, data, "default").rca.cross_domain_relationships)
    example = " ".join(_run(tmp_path / "b", incident, data, EXAMPLE).rca.cross_domain_relationships)
    assert "Within 30 min of the incident" in default        # 25 min after, inside 30
    assert "more than 20 min after the incident" in example  # outside the site's 20-min window


def test_run_records_reported_time(adapter, gateway, scopes):
    run = run_diagnosis(adapter.load_incident("POC-SCHEDULER-001"), gateway, scopes)
    assert run.incident_reported_at is not None and run.site == "default"
