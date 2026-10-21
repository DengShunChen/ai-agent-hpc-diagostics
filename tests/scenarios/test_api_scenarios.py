"""End-to-end review lifecycles through the HTTP API."""


def test_gpu_001_approve_and_close(api, service):
    auth = api.auth_header()
    assert api.get("/api/whoami", headers=auth).json() == {"operator": "ops.wang"}
    assert "POC-GPU-001" in api.get("/api/incidents", headers=auth).json()

    created = api.post("/api/runs", json={"incident_id": "POC-GPU-001"}, headers=auth)
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    assert created.json()["status"] == "AWAITING_HUMAN_APPROVAL"

    review = api.get(f"/api/runs/{run_id}/review", headers=auth).json()
    assert review["classification"] == "gpu" and review["actions"] == []
    assert "this platform executes nothing" in review["note"]
    assert any("XID 13" in h for h in review["hypotheses"])

    run = api.get(f"/api/runs/{run_id}", headers=auth).json()
    assert all(c["status"] != "confirmed" for c in run["rca"]["probable_causes"])

    decided = api.post(f"/api/runs/{run_id}/decision", headers=auth,
                       json={"decision": "APPROVED", "comment": "agree: software first"})
    assert decided.status_code == 200 and decided.json()["status"] == "APPROVED"
    again = api.post(f"/api/runs/{run_id}/decision", headers=auth, json={"decision": "APPROVED"})
    assert again.status_code == 409

    assert api.post(f"/api/runs/{run_id}/close", headers=auth, json={}).json()["status"] == "CLOSED"
    statuses = [h["status"] for h in api.get(f"/api/runs/{run_id}/history", headers=auth).json()]
    assert statuses[-2:] == ["APPROVED", "CLOSED"]
    assert api.get("/api/audit/verify", headers=auth).json()["chain"] == "ok"


def test_storage_001_reject_and_revise(api):
    auth = api.auth_header("ops.lee")
    run_id = api.post("/api/runs", json={"incident_id": "POC-STORAGE-001"}, headers=auth).json()["run_id"]
    no_comment = api.post(f"/api/runs/{run_id}/decision", headers=auth, json={"decision": "REJECTED"})
    assert no_comment.status_code == 422
    api.post(f"/api/runs/{run_id}/decision", headers=auth,
             json={"decision": "REJECTED", "comment": "need per-OST utilization"})
    revised = api.post(f"/api/runs/{run_id}/revise", headers=auth, json={"comment": "re-run"})
    assert revised.status_code == 201 and revised.json()["supersedes"] == run_id
    runs = api.get("/api/runs", headers=auth).json()
    assert [r["run_id"] for r in runs][:2] == [revised.json()["run_id"], run_id]
    assert {r["status"] for r in runs} == {"AWAITING_HUMAN_APPROVAL", "REVISE"}


def test_unknown_run_and_incident(api):
    auth = api.auth_header()
    assert api.get("/api/runs/run-000000000000/review", headers=auth).status_code == 404
    assert api.post("/api/runs", json={"incident_id": "POC-NOPE-999"}, headers=auth).status_code == 404


def test_review_page_served(api):
    page = api.get("/")
    assert page.status_code == 200 and "記錄決定不會執行任何動作" in page.text
    assert '<html lang="zh-Hant">' in page.text
    assert api.get("/static/app.js").headers["content-type"].startswith("text/javascript")
    assert api.get("/healthz").json() == {"status": "ok"}
