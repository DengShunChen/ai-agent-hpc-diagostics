import pytest

from hpc_agent.agents import TriageAgent
from hpc_agent.models import Domain, Incident


@pytest.mark.parametrize(
    "incident_id,classification,related,agents",
    [
        ("POC-STORAGE-001", "storage", ["scheduler"], ["storage-agent", "scheduler-agent"]),
        ("POC-SCHEDULER-001", "scheduler", [], ["scheduler-agent"]),
        ("POC-GPU-001", "gpu", ["scheduler"], ["gpu-agent", "scheduler-agent"]),
    ],
)
def test_scenario_classification(adapter, incident_id, classification, related, agents):
    result = TriageAgent().triage(adapter.load_incident(incident_id))
    assert result.classification == classification
    assert result.related_domains == related
    assert result.selected_agents == agents


def test_signals_reference_real_incident_items(adapter):
    incident = adapter.load_incident("POC-STORAGE-001")
    result = TriageAgent().triage(incident)
    assert result.domain_signals[Domain.STORAGE] == ["POC-STORAGE-001:symptoms[0]", "POC-STORAGE-001:symptoms[1]"]
    assert result.domain_signals[Domain.SCHEDULER] == ["POC-STORAGE-001:symptoms[0]"]
    assert [e.statement for e in result.available_evidence] == incident.symptoms


def test_storage_001_missing_evidence(adapter):
    result = TriageAgent().triage(adapter.load_incident("POC-STORAGE-001"))
    assert result.missing_evidence == [
        "incident timestamp", "affected node name", "job ID",
        "monitoring alerts for the incident window", "relevant log excerpts",
    ]


def test_unknown_when_no_domain_signal():
    result = TriageAgent().triage(Incident(incident_id="INC-001", severity="low", symptoms=["users unhappy"]))
    assert result.classification is Domain.UNKNOWN
    assert result.selected_agents == []
    assert "symptoms specific enough to identify an affected domain" in result.missing_evidence


def test_tie_is_multi_domain():
    incident = Incident(incident_id="INC-001", severity="high", symptoms=["CUDA error", "OST unreachable"])
    result = TriageAgent().triage(incident)
    assert result.classification is Domain.MULTI_DOMAIN
    assert set(result.related_domains) == {Domain.GPU, Domain.STORAGE, Domain.NETWORK}


def test_empty_incident_is_unknown():
    result = TriageAgent().triage(Incident(incident_id="INC-001", severity="low"))
    assert result.classification is Domain.UNKNOWN and result.available_evidence == []
