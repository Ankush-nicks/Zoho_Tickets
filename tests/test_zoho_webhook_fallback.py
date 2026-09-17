from fastapi.testclient import TestClient

from app import config, db, classifier, memory
from app.main import app
from app.models import ClassificationResult

WEBHOOK_SECRET = "test-webhook-secret"


def _headers():
    return {"X-Webhook-Secret": WEBHOOK_SECRET}


def _patch_common(monkeypatch):
    monkeypatch.setattr(config, "ZOHO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")


def test_new_ticket_success_never_flags_fallback(isolated_db, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        classifier,
        "classify",
        lambda *a, **k: ClassificationResult(
            category_id="G01-S01", confidence=0.9, reasoning="clear match", needs_clarification=False
        ),
    )
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={"zoho_ticket_id": "Z-1", "issue_in_detail": "Feedback was too generic"},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["needs_review"] is False
    assert body["category_of_the_issue"]
    assert body["sub_category_of_the_issue"]

    ticket = db.get_ticket_by_zoho_id("Z-1")
    assert ticket["fallback_reason"] is None
    assert ticket["category_id"] == "G01-S01"


def test_new_ticket_classify_failure_still_returns_valid_category(isolated_db, monkeypatch):
    _patch_common(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("OpenRouter is down")

    monkeypatch.setattr(classifier, "classify", _boom)
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={"zoho_ticket_id": "Z-2", "issue_in_detail": "Something broke"},
        headers=_headers(),
    )

    # Never a 500, and never a blank/null mandatory field, even though
    # classify() itself raised.
    assert response.status_code == 200
    body = response.json()
    assert body["needs_review"] is True
    assert body["category_of_the_issue"]
    assert body["sub_category_of_the_issue"]

    ticket = db.get_ticket_by_zoho_id("Z-2")
    assert ticket["status"] == "needs_human_review"
    assert ticket["fallback_reason"].startswith("classify_error:")


def test_existing_ticket_with_orphaned_category_id_falls_back(isolated_db, monkeypatch):
    _patch_common(monkeypatch)
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-3")
    db.update_ticket(ticket_id, status="classified", category_id="NO-SUCH-ID")
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={"zoho_ticket_id": "Z-3", "issue_in_detail": "an edit came in"},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["updated"] is True
    assert body["needs_review"] is True
    assert body["category_of_the_issue"]
    assert body["sub_category_of_the_issue"]

    ticket = db.get_ticket(ticket_id)
    assert ticket["fallback_reason"] == "orphaned_category_id:NO-SUCH-ID"
    # The stored (now-orphaned) id is left untouched - only the write-back
    # response and the observability flag change, never the ticket's own data.
    assert ticket["category_id"] == "NO-SUCH-ID"


def test_existing_ticket_never_classified_falls_back(isolated_db, monkeypatch):
    _patch_common(monkeypatch)
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-4")
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={"zoho_ticket_id": "Z-4", "issue_in_detail": "an edit came in before classification ran"},
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["needs_review"] is True
    assert body["category_of_the_issue"]
    assert body["sub_category_of_the_issue"]

    ticket = db.get_ticket(ticket_id)
    assert ticket["fallback_reason"] == "never_classified"


def test_correct_endpoint_clears_fallback_reason(isolated_db, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(memory, "add_example", lambda *a, **k: None)
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-5")
    db.update_ticket(ticket_id, status="needs_human_review", fallback_reason="never_classified")
    client = TestClient(app)
    client.post("/api/login", json={"username": "admin", "password": "admin"})

    response = client.post(f"/api/tickets/{ticket_id}/correct", json={"corrected_category_id": "G01-S01"})

    assert response.status_code == 200
    ticket = db.get_ticket(ticket_id)
    assert ticket["fallback_reason"] is None
