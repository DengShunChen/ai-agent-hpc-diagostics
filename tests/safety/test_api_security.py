"""HTTP API: authentication, fail-closed behavior, input handling and headers."""

import pytest
from fastapi.testclient import TestClient

from hpc_agent.api.routes import MAX_BODY_BYTES, create_app

AUTHENTICATED = [
    ("GET", "/api/whoami", None), ("GET", "/api/incidents", None), ("GET", "/api/runs", None),
    ("POST", "/api/runs", {"incident_id": "POC-GPU-001"}), ("GET", "/api/runs/run-000000000000/review", None),
    ("GET", "/api/runs/run-000000000000/history", None),
    ("POST", "/api/runs/run-000000000000/decision", {"decision": "APPROVED"}),
    ("POST", "/api/runs/run-000000000000/close", {}), ("POST", "/api/runs/run-000000000000/revise", {}),
    ("GET", "/api/audit/verify", None),
]


@pytest.mark.parametrize("method,path,body", AUTHENTICATED)
@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic b3BzOndhbmc=", "Bearer ", "ops.wang"])
def test_requires_valid_token(api, service, method, path, body, header):
    headers = {"Authorization": header} if header else {}
    response = api.request(method, path, json=body, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert service.audit.verify() == 0 and service.approvals.verify() == 0


@pytest.mark.parametrize("method,path,body", AUTHENTICATED)
def test_fails_closed_without_token_store(service, method, path, body):
    client = TestClient(create_app(service, None))
    response = client.request(method, path, json=body, headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503
    assert service.audit.verify() == 0


def test_unauthenticated_requests_change_nothing(api, service):
    api.post("/api/runs", json={"incident_id": "POC-GPU-001"})
    assert service.audit.verify() == 0 and service.approvals.verify() == 0


def test_operator_identity_comes_from_token_not_body(api, service):
    run_id = api.post("/api/runs", json={"incident_id": "POC-GPU-001"}, headers=api.auth_header()).json()["run_id"]
    response = api.post(f"/api/runs/{run_id}/decision", headers=api.auth_header("ops.lee"),
                        json={"decision": "APPROVED", "comment": "ok", "operator": "ops.wang"})
    assert response.status_code == 422  # extra fields are rejected
    response = api.post(f"/api/runs/{run_id}/decision", headers=api.auth_header("ops.lee"),
                        json={"decision": "APPROVED", "comment": "ok"})
    assert response.status_code == 200 and response.json()["operator"] == "ops.lee"
    assert response.json()["executed"] is False


@pytest.mark.parametrize("decision", ["CLOSED", "REVISE", "DRAFT", "AUTO_APPROVED", "approved", ""])
def test_only_human_decisions_accepted(api, decision):
    run_id = api.post("/api/runs", json={"incident_id": "POC-GPU-001"}, headers=api.auth_header()).json()["run_id"]
    response = api.post(f"/api/runs/{run_id}/decision", headers=api.auth_header(), json={"decision": decision})
    assert response.status_code == 422


def test_secret_in_comment_not_echoed_or_stored(api, service):
    run_id = api.post("/api/runs", json={"incident_id": "POC-GPU-001"}, headers=api.auth_header()).json()["run_id"]
    before = service.approvals.verify()
    response = api.post(f"/api/runs/{run_id}/decision", headers=api.auth_header(),
                        json={"decision": "REJECTED", "comment": "use password=hunter2-not-real"})
    assert response.status_code == 422
    assert "hunter2" not in response.text
    assert service.approvals.verify() == before
    assert "hunter2" not in service.approvals.path.read_text() + service.audit.path.read_text()


def test_invalid_body_not_echoed(api):
    response = api.post("/api/runs", headers=api.auth_header(), json={"incident_id": "../../etc/passwd; rm -rf /"})
    assert response.status_code == 422 and "rm -rf" not in response.text


@pytest.mark.parametrize("path", ["/api/runs/run-..%2F..%2Fetc/review", "/api/runs/..%2Fsecrets/history",
                                  "/api/runs/RUN-ABC/review", "/static/..%2F..%2Froutes.py", "/static/index.html"])
def test_path_traversal_rejected(api, path):
    assert api.get(path, headers=api.auth_header()).status_code == 404


def test_body_size_limit(api):
    big = '{"incident_id": "POC-GPU-001", "pad": "' + "x" * MAX_BODY_BYTES + '"}'
    response = api.post("/api/runs", headers=api.auth_header() | {"Content-Type": "application/json"}, content=big)
    assert response.status_code == 413


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_disabled(api, path):
    assert api.get(path).status_code == 404


def test_security_headers_everywhere(api):
    for response in (api.get("/"), api.get("/healthz"), api.get("/api/runs"), api.get("/static/app.js")):
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"


def test_no_execution_endpoints(api):
    paths = {route.path for route in api.app.routes}
    assert not any(word in p for p in paths for word in ("exec", "shell", "ssh", "command", "remediat", "apply"))


def test_ui_renders_without_html_injection():
    from pathlib import Path

    static = Path(__file__).resolve().parents[2] / "src" / "hpc_agent" / "api" / "static"
    js = (static / "app.js").read_text()
    html = (static / "index.html").read_text()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
    assert "eval(" not in js and "new Function" not in js and "localStorage" not in js
    assert "<script>" not in html and "style=" not in html and "onclick" not in html
