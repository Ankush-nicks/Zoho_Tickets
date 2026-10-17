"""
The Log page must not read every ticket's full row (raw Zoho payload, full
context) - on Turso that is the whole table over the network on each load.
Its two requests use db.list_log_tickets(): only the columns they need, with
uploaded history filtered out in SQL.
"""
import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as main_module
from app.main import app
from tests.test_routing_log import LEAF

UPLOAD_REASONING = main_module._zoho_category_fields("Feedback Too Generic or Vague")["reasoning"]


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


def _make(db, zid, reasoning, raw):
    tid = db.create_ticket("issue " + zid, zoho_ticket_id=zid, raw_payload=raw)
    db.update_ticket(tid, status="classified", category_id=LEAF, reasoning=reasoning)
    return tid


@pytest.fixture()
def mixed(isolated_db):
    """Upload history plus the three look-alikes that must stay in the Log."""
    return {
        "upload": _make(isolated_db, "U-1", UPLOAD_REASONING, {"added_time": "01/09/2026 10:00:00"}),
        "upload_null_time": _make(isolated_db, "U-2", UPLOAD_REASONING, {"added_time": None}),
        "live": _make(isolated_db, "L-1", "Model reasoning", {"ticket_status": "Open"}),
        "refreshed": _make(isolated_db, "L-2", "Model reasoning", {"added_time": "01/09/2026 10:00:00"}),
        "went_live": _make(isolated_db, "L-3", UPLOAD_REASONING, {"ticket_status": "Open"}),
        "no_payload": _make(isolated_db, "L-4", "Model reasoning", None),
    }


def test_log_endpoints_never_read_full_rows(client, isolated_db, mixed, monkeypatch):
    def boom():
        raise AssertionError("the Log page must not read every full ticket row")

    monkeypatch.setattr(isolated_db, "list_all_tickets", boom)
    log = client.get("/api/ticket-log")
    routing = client.get("/api/routing-log")
    assert log.status_code == 200 and routing.status_code == 200
    assert {t["zoho_ticket_id"] for t in log.json()["tickets"]} == {"L-1", "L-2", "L-3", "L-4"}
    assert "summary" in routing.json()


def test_sql_filter_matches_is_upload_history(isolated_db, mixed):
    expected = {t["id"] for t in isolated_db.list_all_tickets() if not main_module._is_upload_history(t)}
    got = {t["id"] for t in isolated_db.list_log_tickets(main_module._UPLOAD_REASONING_MARK)}
    assert got == expected
    assert mixed["upload"] not in got and mixed["upload_null_time"] not in got


def test_log_rows_carry_no_payload(isolated_db, mixed):
    row = isolated_db.list_log_tickets(main_module._UPLOAD_REASONING_MARK)[0]
    assert "raw_payload" not in row and "full_context" not in row
    assert {"id", "zoho_ticket_id", "created_at", "category_id", "decision", "original_text"} <= set(row)


def test_log_rows_refresh_after_a_write(isolated_db, mixed):
    mark = main_module._UPLOAD_REASONING_MARK
    before = len(isolated_db.list_log_tickets(mark))
    _make(isolated_db, "L-9", "Model reasoning", None)
    assert len(isolated_db.list_log_tickets(mark)) == before + 1


def test_list_request_leaves_the_cached_rows_in_order(client, isolated_db, mixed):
    mark = main_module._UPLOAD_REASONING_MARK
    client.get("/api/ticket-log")  # sorts newest first for the page
    times = [t["created_at"] for t in isolated_db.list_log_tickets(mark)]
    assert times == sorted(times)  # the shared cache is still oldest first
