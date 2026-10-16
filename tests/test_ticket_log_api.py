"""GET /api/ticket-log and /api/ticket-log/{id}: the Log page's list and timelines."""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from tests.test_routing_log import GROUP_NAME, LEAF, LEAF_NAME


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


def _ticket(db, zid, text="Cannot open the course page\nsecond line", category_id=LEAF):
    tid = db.create_ticket(text, zoho_ticket_id=zid)
    if category_id:
        db.update_ticket(tid, category_id=category_id)
    return tid


def test_list_and_timeline(client, isolated_db):
    tid = _ticket(isolated_db, "E2E-1")
    isolated_db.add_event(tid, "presubmit", "Suggested something.", duration_ms=420)
    bare = _ticket(isolated_db, "E2E-2")
    body = client.get("/api/ticket-log").json()
    assert body["total"] == 2
    by_id = {t["id"]: t for t in body["tickets"]}
    one = by_id[tid]
    assert one["zoho_ticket_id"] == "E2E-1"
    assert one["first_line"] == "Cannot open the course page"
    assert one["final"] == f"{GROUP_NAME} › {LEAF_NAME}"
    assert one["has_timeline"] is True and one["has_error"] is False and one["submitted"] is True
    assert by_id[bare]["has_timeline"] is False
    detail = client.get(f"/api/ticket-log/{tid}").json()
    assert detail["ticket"]["issue_text"].startswith("Cannot open the course page\nsecond")
    assert [e["kind"] for e in detail["events"]] == ["presubmit"]
    assert client.get("/api/ticket-log/nope").status_code == 404


def test_search_by_zoho_id(client, isolated_db):
    _ticket(isolated_db, "E2E-1")
    _ticket(isolated_db, "ABC-9")
    body = client.get("/api/ticket-log", params={"q": "e2e"}).json()
    assert [t["zoho_ticket_id"] for t in body["tickets"]] == ["E2E-1"]


def test_errors_only(client, isolated_db):
    bad = _ticket(isolated_db, "Z-1")
    isolated_db.add_event(bad, "error", "It failed.", error="boom")
    _ticket(isolated_db, "Z-2")
    body = client.get("/api/ticket-log", params={"errors_only": "true"}).json()
    assert [t["id"] for t in body["tickets"]] == [bad]
    assert body["tickets"][0]["has_error"] is True


def test_unsubmitted_lists_drafts(client, isolated_db):
    _ticket(isolated_db, "Z-1")
    draft = isolated_db.create_draft("Draft text", None)
    isolated_db.add_event(draft, "presubmit", "Suggested.")
    body = client.get("/api/ticket-log", params={"unsubmitted": "true"}).json()
    assert [t["id"] for t in body["tickets"]] == [draft]
    assert body["tickets"][0]["submitted"] is False
    detail = client.get(f"/api/ticket-log/{draft}").json()
    assert detail["ticket"]["submitted"] is False and len(detail["events"]) == 1


def test_first_line_is_trimmed(client, isolated_db):
    _ticket(isolated_db, "Z-1", text="x" * 45000)
    body = client.get("/api/ticket-log").json()
    assert len(body["tickets"][0]["first_line"]) <= 141


def test_requires_login(isolated_db):
    c = TestClient(app)
    assert c.get("/api/ticket-log").status_code == 401
    assert c.get("/api/ticket-log/x").status_code == 401
