"""LLM explanation through the API, with a fake local provider (no real model needed)."""

from fastapi.testclient import TestClient

from hpc_agent.api.routes import create_app
from tests.safety.test_llm_guardrails import GOOD, FakeProvider


def client_with(service, operator_tokens, llm):
    tokens, issued = operator_tokens
    client = TestClient(create_app(service, tokens, llm))
    return client, {"Authorization": f"Bearer {issued['ops.wang']}"}


def test_explanation_requires_configured_llm(service, operator_tokens):
    client, auth = client_with(service, operator_tokens, None)
    run_id = client.post("/api/runs", json={"incident_id": "POC-SCHEDULER-001"}, headers=auth).json()["run_id"]
    assert client.get("/api/llm", headers=auth).json()["configured"] is False
    response = client.post(f"/api/runs/{run_id}/explanation", headers=auth, json={})
    assert response.status_code == 503
    assert client.get(f"/api/runs/{run_id}/explanation", headers=auth).json() == {"explanation": None}


def test_explanation_flow(service, operator_tokens):
    client, auth = client_with(service, operator_tokens, FakeProvider(GOOD))
    run_id = client.post("/api/runs", json={"incident_id": "POC-SCHEDULER-001"}, headers=auth).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/explanation", json={}).status_code == 401
    created = client.post(f"/api/runs/{run_id}/explanation", headers=auth, json={})
    assert created.status_code == 201 and created.json()["status"] == "accepted"
    latest = client.get(f"/api/runs/{run_id}/explanation", headers=auth).json()["explanation"]
    assert latest["text"] == GOOD and "不是證據" in latest["disclaimer"]
    review = client.get(f"/api/runs/{run_id}/review", headers=auth).json()
    assert review["status"] == "AWAITING_HUMAN_APPROVAL"  # explanation changes no approval state
    audit = [a for a in service.audit.records() if a["agents_invoked"] == ["evidence-explainer"]]
    assert audit[-1]["operator"] == "ops.wang"


def test_rejected_explanation_hides_text(service, operator_tokens):
    client, auth = client_with(service, operator_tokens, FakeProvider("根本原因是節點故障 [H2]。"))
    run_id = client.post("/api/runs", json={"incident_id": "POC-SCHEDULER-001"}, headers=auth).json()["run_id"]
    body = client.post(f"/api/runs/{run_id}/explanation", headers=auth, json={}).json()
    assert body["status"] == "rejected" and body["text"] is None
    assert any("confirmed root cause" in r for r in body["rejection_reasons"])
