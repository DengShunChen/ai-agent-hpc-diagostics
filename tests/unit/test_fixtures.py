import pytest

from hpc_agent.models import Incident

SCENARIOS = ["POC-STORAGE-001", "POC-SCHEDULER-001", "POC-GPU-001"]


def test_all_scenario_fixtures_present(adapter):
    assert set(SCENARIOS) <= set(adapter.list_incident_ids())


@pytest.mark.parametrize("incident_id", SCENARIOS)
def test_fixture_loads_as_incident(adapter, incident_id):
    incident = adapter.load_incident(incident_id)
    assert isinstance(incident, Incident)
    assert incident.incident_id == incident_id
    assert incident.symptoms


def test_storage_fixture_matches_spec(adapter):
    incident = adapter.load_incident("POC-STORAGE-001")
    assert incident.severity == "high"
    assert incident.symptoms == ["job failed while writing output", "filesystem utilization is high"]
    assert incident.alerts == incident.logs == incident.operator_notes == []


def test_missing_fixture_raises(adapter):
    with pytest.raises(FileNotFoundError):
        adapter.load_incident("POC-NOPE-999")
