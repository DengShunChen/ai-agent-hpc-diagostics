import pytest
from pydantic import ValidationError

from hpc_agent.models import Incident, Severity


def test_minimal_incident_defaults():
    incident = Incident(incident_id="INC-001", severity="high")
    assert incident.system == "HPC"
    assert incident.severity is Severity.HIGH
    assert incident.node is None and incident.timestamp is None
    assert incident.symptoms == [] and incident.alerts == []


def test_missing_incident_id_rejected():
    with pytest.raises(ValidationError):
        Incident(severity="high")


@pytest.mark.parametrize("bad_id", ["", "inc-001", "../etc/passwd", "INC 001", "X"])
def test_malformed_incident_id_rejected(bad_id):
    with pytest.raises(ValidationError):
        Incident(incident_id=bad_id, severity="high")


def test_invalid_severity_rejected():
    with pytest.raises(ValidationError):
        Incident(incident_id="INC-001", severity="catastrophic")


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        Incident(incident_id="INC-001", severity="high", hostname_ip="10.0.0.1")


def test_blank_symptom_rejected():
    with pytest.raises(ValidationError):
        Incident(incident_id="INC-001", severity="high", symptoms=["   "])


def test_incident_is_immutable():
    incident = Incident(incident_id="INC-001", severity="high")
    with pytest.raises(ValidationError):
        incident.severity = "low"
