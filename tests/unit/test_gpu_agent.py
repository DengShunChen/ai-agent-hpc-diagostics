import pytest

from hpc_agent.agents import GpuAgent
from hpc_agent.tools.gateway import ScopedGateway, build_default_gateway
from tests.conftest import make_data_root

INCIDENT = {"incident_id": "INC-GPU-001", "severity": "high", "node": "sim-gpu-node-0001",
            "symptoms": ["training job aborted with a CUDA error"]}
CLEAN_TELEMETRY = {"temperature_c": 60, "power_w": 250, "ecc_dbe_volatile": 0, "pcie_replay_count": 0,
                   "nvlink_errors": 0}


def gpu(index=0, health="pass", xids=(), **fields):
    return {"index": index, "health": health, "xid_events": [{"xid": x, "count": 1} for x in xids]} | fields


def diagnose(tmp_path, scopes, gpus=None):
    data = {"gpu/gpu_health": {"_synthetic": True, "node": "sim-gpu-node-0001", "gpus": gpus}} if gpus is not None else None
    adapter = make_data_root(tmp_path, INCIDENT, data)
    gateway = ScopedGateway(build_default_gateway(adapter), "gpu-agent", scopes, [])
    return GpuAgent().diagnose(adapter.load_incident(INCIDENT["incident_id"]), gateway)


def hardware_causes(d):
    return [c for c in d.hypotheses if c.cause.startswith("GPU hardware fault")]


def test_no_data(tmp_path, scopes):
    d = diagnose(tmp_path, scopes)
    assert "GPU involvement is not determined" in d.finding
    assert "get_gpu_health()" in d.recommended_checks
    assert hardware_causes(d) == []


def test_all_pass(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(i, **CLEAN_TELEMETRY) for i in range(4)])
    assert "not supported" in d.finding
    assert d.affected_components == []
    assert all(c.cause.startswith("Application or CUDA") is False for c in d.hypotheses)


def test_app_xid_without_telemetry_is_not_hardware(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(2, "warn", [13])])
    assert hardware_causes(d) == []
    [cause] = d.hypotheses
    assert "XID 13 (graphics engine exception)" in cause.cause and "commonly associated" in cause.cause
    assert "No hardware failure can be concluded: hardware telemetry is incomplete" in d.finding
    assert any("ECC double-bit error counter" in m for m in d.missing_information)
    assert d.recommended_actions == []
    assert d.affected_components == ["node:sim-gpu-node-0001", "gpu:sim-gpu-node-0001/2"]


def test_app_xid_with_clean_telemetry(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(0, "warn", [31], **CLEAN_TELEMETRY)])
    assert hardware_causes(d) == []
    assert "no hardware fault indicators are present" in d.finding


@pytest.mark.parametrize("xid", [48, 79, 95])
def test_hardware_xid_gives_hypothesis_and_proposal(tmp_path, scopes, xid):
    d = diagnose(tmp_path, scopes, [gpu(1, "fail", [xid], **CLEAN_TELEMETRY)])
    [cause] = hardware_causes(d)
    assert "needed before any hardware conclusion" in cause.cause
    assert cause.confidence == 0.5
    [action] = d.recommended_actions
    assert action.action.startswith("Drain sim-gpu-node-0001") and action.risk == "medium"
    assert "not confirmed" in d.finding


def test_ecc_counter_is_hardware_indicator(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(0, "warn", **(CLEAN_TELEMETRY | {"ecc_dbe_volatile": 2}))])
    assert hardware_causes(d)


def test_thermal(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(0, "warn", **(CLEAN_TELEMETRY | {"temperature_c": 91}))])
    assert any("Thermal" in c.cause for c in d.hypotheses)


def test_unknown_and_follow_on_xids(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(0, "warn", [999, 45], **CLEAN_TELEMETRY)])
    assert "interpretation of XID 999 (not in the agent's XID table)" in d.missing_information
    assert any("events preceding XID 45" in m for m in d.missing_information)
    assert hardware_causes(d) == []


def test_unexplained_warning_per_gpu(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(0, "warn", [13]), gpu(1, "warn", **CLEAN_TELEMETRY)])
    assert any("without a recorded cause" in c.cause and "GPU 1" in c.cause for c in d.hypotheses)


def test_never_recommends_running_dcgm_itself(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [gpu(0, "warn", [13])])
    dcgm = [c for c in d.recommended_checks if "DCGM" in c]
    assert dcgm and all("not executed by this platform" in c for c in dcgm)
