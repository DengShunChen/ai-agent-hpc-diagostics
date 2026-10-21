import pytest

from hpc_agent.agents import NetworkAgent
from hpc_agent.tools.gateway import ScopedGateway, build_default_gateway
from tests.conftest import make_data_root

INCIDENT = {"incident_id": "INC-NET-001", "severity": "high", "node": "sim-node-0005",
            "symptoms": ["MPI job reports network timeout"]}


def iface(**overrides):
    return {"node": "sim-node-0005", "interface": "bond0", "state": "up", "rx_errors": 0, "tx_errors": 0,
            "link_flaps": 0} | overrides


def diagnose(tmp_path, scopes, interfaces=None):
    data = {"network/network_status": {"interfaces": interfaces}} if interfaces is not None else None
    adapter = make_data_root(tmp_path, INCIDENT, data)
    gateway = ScopedGateway(build_default_gateway(adapter), "network-agent", scopes, [])
    return NetworkAgent().diagnose(adapter.load_incident(INCIDENT["incident_id"]), gateway)


def test_no_data(tmp_path, scopes):
    d = diagnose(tmp_path, scopes)
    assert d.possible_causes == [] and d.confidence == 0.0
    assert "network involvement is not determined" in d.finding
    assert d.recommended_checks == ["get_network_status()"]


def test_healthy_interfaces_do_not_support_network_cause(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [iface()])
    assert d.possible_causes == []
    assert "not supported" in d.finding
    assert d.affected_components == []


def test_link_down(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [iface(state="down")])
    [cause] = d.hypotheses
    assert "sim-node-0005/bond0 is down" in cause.cause and cause.confidence == 0.5
    assert d.affected_components == ["node:sim-node-0005", "interface:sim-node-0005/bond0"]
    assert "switch-side port status for sim-node-0005/bond0" in d.missing_information
    assert d.recommended_actions == []


def test_degraded_bond_is_low_confidence(tmp_path, scopes):
    members = [{"interface": "eth0", "state": "up"}, {"interface": "eth1", "state": "down"}]
    d = diagnose(tmp_path, scopes, [iface(members=members)])
    [cause] = d.hypotheses
    assert "degraded" in cause.cause and "does not indicate an outage" in cause.cause
    assert cause.confidence == 0.2


def test_errors_and_flaps(tmp_path, scopes):
    d = diagnose(tmp_path, scopes, [iface(rx_errors=120, tx_errors=3, link_flaps=4)])
    assert len(d.hypotheses) == 2
    assert any("single snapshot" in m for m in d.missing_information)
    assert any(e.statement == "sim-node-0005/bond0 rx_errors=120 tx_errors=3" for e in d.evidence)


@pytest.mark.parametrize("field,value", [("rx_errors", "many"), ("state", None)])
def test_malformed_fields_are_missing(tmp_path, scopes, field, value):
    d = diagnose(tmp_path, scopes, [iface(**{field: value})])
    assert any("sim-node-0005/bond0" in m for m in d.missing_information)
    assert d.possible_causes == []
