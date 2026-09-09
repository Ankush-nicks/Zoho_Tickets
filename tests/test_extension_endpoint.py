from fastapi.testclient import TestClient

from app import config, db
from app.main import app

POC_EMAIL = "ranjith.kumar@nxtwave.co.in"


def test_missing_token_returns_401(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")
    client = TestClient(app)

    response = client.get("/api/extension/my-tickets")

    assert response.status_code == 401


def test_wrong_token_returns_401(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")
    client = TestClient(app)

    response = client.get("/api/extension/my-tickets", headers={"X-POC-Token": "wrong"})

    assert response.status_code == 401


def test_valid_token_returns_this_pocs_queue(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")
    ticket_id = db.create_ticket("some issue text", created_at=1_000_000.0 - 3600)
    db.update_ticket(ticket_id, status="classified", category_id="G01-S01")
    client = TestClient(app)

    response = client.get("/api/extension/my-tickets", headers={"X-POC-Token": "secret-token"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["tickets"]) == 1
    assert body["tickets"][0]["category_group_code"] == "G01"
    assert "summary" in body
