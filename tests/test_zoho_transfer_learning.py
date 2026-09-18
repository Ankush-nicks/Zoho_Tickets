"""
Tests for the webhook's "ticket transfer" learning path: when a human
changes sub_category_of_the_issue on an already-classified ticket in Zoho
(routing it to the right team after we got it wrong), that edit should be
treated as an implicit correction - logged to app/db.py's corrections table
and fed into app/memory.py's vector memory, exactly like the in-portal
"Correct" button - not just silently refreshed on the ticket record.

Uses real taxonomy.json leaves (G01-S01/S02/S03) rather than fake ids, since
_resolve_taxonomy_leaf_by_name has to match against the actual taxonomy.
"""
from fastapi.testclient import TestClient

from app import config, db, memory
from app.main import app

WEBHOOK_SECRET = "test-webhook-secret"


def _headers():
    return {"X-Webhook-Secret": WEBHOOK_SECRET}


def _patch_common(monkeypatch):
    monkeypatch.setattr(config, "ZOHO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")


def test_genuine_transfer_logs_correction_and_updates_memory(isolated_db, monkeypatch):
    _patch_common(monkeypatch)
    added_examples = []
    monkeypatch.setattr(
        memory, "add_example",
        lambda text, category_id, api_key, source="correction", ticket_id=None:
            added_examples.append({"text": text, "category_id": category_id, "ticket_id": ticket_id}),
    )
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-100")
    db.update_ticket(
        ticket_id, status="classified", category_id="G01-S01",
        zoho_subcategory="Feedback Too Generic or Vague",
    )
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={
            "zoho_ticket_id": "Z-100",
            "issue_in_detail": "original text",
            "sub_category_of_the_issue": "QA Evaluation Prompt Update Request",  # = G01-S02
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["sub_category_of_the_issue"] == "QA Evaluation Prompt Update Request"
    assert body["needs_review"] is False

    ticket = db.get_ticket(ticket_id)
    assert ticket["category_id"] == "G01-S02"
    assert ticket["status"] == "corrected"

    corrections = db.list_corrections()
    assert len(corrections) == 1
    assert corrections[0]["predicted_category_id"] == "G01-S01"
    assert corrections[0]["corrected_category_id"] == "G01-S02"
    assert corrections[0]["corrected_by"] == "zoho-transfer"

    assert len(added_examples) == 1
    assert added_examples[0]["category_id"] == "G01-S02"
    assert added_examples[0]["ticket_id"] == ticket_id


def test_first_ever_subcategory_is_not_a_transfer(isolated_db, monkeypatch):
    """No prior zoho_subcategory stored yet - nothing to have been
    'transferred' from, so this must not fire even though it's the ticket's
    first real category."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(memory, "add_example", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-101")
    db.update_ticket(ticket_id, status="classified", category_id="G01-S01")  # zoho_subcategory left unset
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={
            "zoho_ticket_id": "Z-101",
            "issue_in_detail": "original text",
            "sub_category_of_the_issue": "QA Evaluation Prompt Update Request",
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    ticket = db.get_ticket(ticket_id)
    assert ticket["category_id"] == "G01-S01"  # untouched
    assert ticket["status"] == "classified"
    assert db.list_corrections() == []


def test_unchanged_subcategory_is_not_a_transfer(isolated_db, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(memory, "add_example", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-102")
    db.update_ticket(
        ticket_id, status="classified", category_id="G01-S01",
        zoho_subcategory="Feedback Too Generic or Vague",
    )
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={
            "zoho_ticket_id": "Z-102",
            "issue_in_detail": "an unrelated field changed",
            "sub_category_of_the_issue": "Feedback Too Generic or Vague",  # same value
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    ticket = db.get_ticket(ticket_id)
    assert ticket["category_id"] == "G01-S01"
    assert ticket["status"] == "classified"
    assert db.list_corrections() == []


def test_zoho_echoing_our_own_prediction_is_not_a_transfer(isolated_db, monkeypatch):
    """Old subcategory was some other (e.g. raiser's original) value, and
    the new one matches what we ALREADY have as category_id - that's Zoho's
    field catching up to our classification, not a human overriding it."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(memory, "add_example", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-103")
    db.update_ticket(
        ticket_id, status="classified", category_id="G01-S01",
        zoho_subcategory="Some Raiser Picked Category",
    )
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={
            "zoho_ticket_id": "Z-103",
            "issue_in_detail": "original text",
            "sub_category_of_the_issue": "Feedback Too Generic or Vague",  # = G01-S01, our own prediction
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    ticket = db.get_ticket(ticket_id)
    assert ticket["category_id"] == "G01-S01"
    assert ticket["status"] == "classified"  # not flipped to "corrected"
    assert db.list_corrections() == []


def test_unresolvable_new_subcategory_is_left_alone(isolated_db, monkeypatch):
    """New subcategory text doesn't match any taxonomy leaf (typo, free
    text, or a category not in our list) - skip rather than guess, and
    never crash the webhook over it."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(memory, "add_example", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    ticket_id = db.create_ticket("original text", zoho_ticket_id="Z-104")
    db.update_ticket(
        ticket_id, status="classified", category_id="G01-S01",
        zoho_subcategory="Feedback Too Generic or Vague",
    )
    client = TestClient(app)

    response = client.post(
        "/api/webhooks/zoho/tickets",
        json={
            "zoho_ticket_id": "Z-104",
            "issue_in_detail": "original text",
            "sub_category_of_the_issue": "Some Completely Unmapped Free Text Category",
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    ticket = db.get_ticket(ticket_id)
    assert ticket["category_id"] == "G01-S01"
    assert ticket["status"] == "classified"
    assert db.list_corrections() == []
