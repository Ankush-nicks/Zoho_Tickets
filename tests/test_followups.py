"""
- Uploaded history is recognised (and left out of the Log).
- A missing OPENROUTER_API_KEY gets the same webhook failsafe as an
  exhausted one instead of a 500.
"""
import pytest
from fastapi.testclient import TestClient

from app import config
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


# --- uploaded history -----------------------------------------------------------

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
