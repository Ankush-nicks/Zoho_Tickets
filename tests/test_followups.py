"""
- Uploaded history is never sent for resolution grading (credits).
- A missing OPENROUTER_API_KEY gets the same webhook failsafe as an
  exhausted one instead of a 500.
- The Daily Issue Check page's groups are one shared, versioned copy.
"""
import pytest
from fastapi.testclient import TestClient

from app import config, quality_scorer
from app import main as main_module
from app.main import app
from app.taxonomy import taxonomy

WEBHOOK_SECRET = "test-webhook-secret"
LEAF = "G01-S01"


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    monkeypatch.setattr(config, "ZOHO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


# --- resolution grading skips uploaded history --------------------------------

def _closed(isolated_db, zid, reasoning, raw_extra=None):
    tid = isolated_db.create_ticket(
        "issue", zoho_ticket_id=zid,
        raw_payload={"ticket_status": "Resolved By POC", **(raw_extra or {})},
    )
    isolated_db.update_ticket(tid, status="classified", category_id=LEAF, reasoning=reasoning)
    return tid


def test_uploaded_history_is_not_graded_but_live_tickets_are(client, isolated_db, monkeypatch):
    uploaded = _closed(isolated_db, "1", main_module._zoho_category_fields(taxonomy.get(LEAF)["name"])["reasoning"],
                       {"added_time": "01/09/2026 10:00:00"})
    live = _closed(isolated_db, "2", "Model reasoning")
    # Live webhook ticket that an upload later refreshed (CSV data, real reasoning).
    refreshed = _closed(isolated_db, "3", "Model reasoning", {"added_time": "01/09/2026 10:00:00"})
    # Uploaded ticket that then got a live Zoho edit - webhook replaced raw_payload.
    went_live = _closed(isolated_db, "4", main_module._zoho_category_fields(taxonomy.get(LEAF)["name"])["reasoning"])

    graded = []

    def fake_score(t, api_key):
        graded.append(t["id"])
        return {"resolution_score": 5.0, "resolution_scored_at": 1.0}

    monkeypatch.setattr(quality_scorer, "score_ticket", fake_score)
    assert client.get("/api/resolutions/pending-count").json()["count"] == 3
    main_module._score_pending_resolutions_batch(10, "sk-or-test")
    assert sorted(graded) == sorted([live, refreshed, went_live])
    assert uploaded not in graded


def test_every_upload_reasoning_is_recognised():
    for sub in ("Feedback Too Generic or Vague", "Not In Taxonomy", None):
        fields = main_module._zoho_category_fields(sub)
        assert main_module._is_upload_history({"reasoning": fields["reasoning"], "raw_payload": {"added_time": "x"}})


# --- missing key -> failsafe, not 500 ----------------------------------------------

def _post(client, payload):
    return client.post("/api/webhooks/zoho/tickets", json=payload, headers={"X-Webhook-Secret": WEBHOOK_SECRET})


def test_presubmit_without_key_echoes_instructor_pick(client, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", None)
    leaf = taxonomy.get(LEAF)
    body = _post(client, {"issue_in_detail": "vague", "category_of_the_issue": leaf["parent_name"],
                          "sub_category_of_the_issue": leaf["name"]})
    assert body.status_code == 200
    assert body.json() == {"ok": True, "category_of_the_issue": leaf["parent_name"],
                           "sub_category_of_the_issue": leaf["name"], "needs_review": True}


def test_presubmit_without_key_or_pick_still_returns_valid_fields(client, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", None)
    r = _post(client, {"issue_in_detail": "vague"})
    fallback = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    assert r.status_code == 200
    assert (r.json()["sub_category_of_the_issue"], r.json()["needs_review"]) == (fallback["name"], True)


def test_on_add_without_key_still_stores_the_ticket(client, isolated_db, monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", None)
    fallback = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    r = _post(client, {"zoho_ticket_id": "Z-NK", "issue_in_detail": "vague",
                       "category_of_the_issue": fallback["parent_name"], "sub_category_of_the_issue": fallback["name"]})
    assert r.status_code == 200 and r.json()["needs_review"] is True
    t = isolated_db.get_ticket_by_zoho_id("Z-NK")
    assert t["status"] == "needs_human_review"
    assert "OPENROUTER_API_KEY" in t["fallback_reason"]


# --- shared Daily Issue Check state --------------------------------------------------

def test_shared_state_versioning(client):
    assert client.get("/api/daily-issue/state").json()["version"] == 0

    first = client.put("/api/daily-issue/state", json={"value": {"nodes": {"a": 1}}, "version": 0})
    assert first.status_code == 200
    assert (first.json()["version"], first.json()["updated_by"]) == (1, "admin")

    stale = client.put("/api/daily-issue/state", json={"value": {"nodes": {"b": 2}}, "version": 0})
    assert stale.status_code == 409
    assert stale.json()["value"] == {"nodes": {"a": 1}}  # the loser gets the current copy

    second = client.put("/api/daily-issue/state", json={"value": {"nodes": {"a": 1, "c": 3}}, "version": 1})
    assert second.json()["version"] == 2
    assert client.get("/api/daily-issue/state").json()["value"] == {"nodes": {"a": 1, "c": 3}}


def test_shared_state_validation_and_login(client):
    assert client.put("/api/daily-issue/state", json={"value": {"x": 1}, "version": 0}).status_code == 400
    assert client.put("/api/daily-issue/state", json={"value": {"nodes": {}}, "version": "x"}).status_code == 400
    anon = TestClient(app)
    assert anon.get("/api/daily-issue/state").status_code == 401
    assert anon.put("/api/daily-issue/state", json={"value": {"nodes": {}}, "version": 0}).status_code == 401
