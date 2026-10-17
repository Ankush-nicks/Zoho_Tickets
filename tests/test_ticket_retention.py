"""
Ticket retention: tickets that arrived more than TICKET_RETENTION_DAYS ago
(60 by default) are deleted with everything stored for them - timeline
events, clarification turns, correction records - drafts included. The
taxonomy, its saved versions and its change history are never touched.
"""
import logging
import time

from app import config
from app import main as main_module
from app.taxonomy import taxonomy

DAY = 24 * 3600


def _ticket(db, zid, age_days, draft=False):
    if draft:
        tid = db.create_draft("draft " + zid, None)
    else:
        tid = db.create_ticket("issue " + zid, zoho_ticket_id=zid)
    db.update_ticket(tid, created_at=time.time() - age_days * DAY)
    db.add_event(tid, "presubmit", "line")
    db.append_turn(tid, "system_question", "Which one?")
    db.log_correction(tid, "G01-S01", "G01-S02", "zoho-transfer")
    return tid


def _count(db, table, ticket_id):
    col = "id" if table == "tickets" else "ticket_id"
    with db._conn() as conn:
        return db._fetchone(conn, f"SELECT COUNT(*) AS n FROM {table} WHERE {col} = ?", (ticket_id,))["n"]


def test_old_tickets_go_with_everything_stored_for_them(isolated_db):
    old = _ticket(isolated_db, "OLD", 61)
    new = _ticket(isolated_db, "NEW", 59)
    removed = isolated_db.delete_tickets_older_than(time.time() - 60 * DAY)
    assert removed == 1
    for table in ("tickets", "ticket_events", "turns", "corrections"):
        assert _count(isolated_db, table, old) == 0, table
        assert _count(isolated_db, table, new) == 1, table


def test_old_drafts_are_deleted_too(isolated_db):
    old_draft = _ticket(isolated_db, "D1", 90, draft=True)
    fresh_draft = _ticket(isolated_db, "D2", 1, draft=True)
    isolated_db.delete_tickets_older_than(time.time() - 60 * DAY)
    assert _count(isolated_db, "tickets", old_draft) == 0
    assert _count(isolated_db, "tickets", fresh_draft) == 1


def test_deletes_in_batches_until_done(isolated_db):
    ids = [_ticket(isolated_db, f"B{i}", 100) for i in range(5)]
    removed = isolated_db.delete_tickets_older_than(time.time() - 60 * DAY, batch_size=2)
    assert removed == 5
    assert all(_count(isolated_db, "tickets", t) == 0 for t in ids)


def test_deleted_tickets_leave_the_log(isolated_db):
    _ticket(isolated_db, "OLD", 61)
    keep = _ticket(isolated_db, "NEW", 1)
    isolated_db.list_log_tickets(main_module._UPLOAD_REASONING_MARK)  # warm the cache
    isolated_db.delete_tickets_older_than(time.time() - 60 * DAY)
    rows = isolated_db.list_log_tickets(main_module._UPLOAD_REASONING_MARK)
    assert [r["id"] for r in rows] == [keep]
    assert [t["id"] for t in isolated_db.list_all_tickets()] == [keep]


def test_taxonomy_and_its_history_are_never_touched(isolated_db):
    data = taxonomy.as_dict()
    taxonomy.save(data)                                   # live copy in taxonomy_store
    isolated_db.put_taxonomy_doc_as("previous-1", data)   # an old saved version
    change = isolated_db.add_taxonomy_change({"G01-S01": ["a", "b"]}, "admin")
    with isolated_db._conn() as conn:                     # make both look ancient
        isolated_db._exec(conn, "UPDATE taxonomy_store SET updated_at = 0")
        isolated_db._exec(conn, "UPDATE taxonomy_changes SET created_at = 0")
    isolated_db.delete_tickets_older_than(time.time() - 60 * DAY)
    assert isolated_db.get_taxonomy_doc() is not None
    with isolated_db._conn() as conn:
        keys = {r["key"] for r in isolated_db._fetchall(conn, "SELECT key FROM taxonomy_store")}
    assert {"live", "previous-1"} <= keys
    assert isolated_db.get_taxonomy_change(change) is not None


def test_cleanup_uses_the_retention_setting_and_logs(isolated_db, monkeypatch, caplog):
    monkeypatch.setattr(config, "TICKET_RETENTION_DAYS", 30)
    gone = _ticket(isolated_db, "A", 31)
    kept = _ticket(isolated_db, "B", 29)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        assert main_module._cleanup_old_tickets() == 1
    assert _count(isolated_db, "tickets", gone) == 0 and _count(isolated_db, "tickets", kept) == 1
    assert "cleanup: removed 1 ticket(s) older than 30 days" in caplog.text


def test_cleanup_never_raises(isolated_db, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(isolated_db, "delete_tickets_older_than", broken)
    assert main_module._cleanup_old_tickets() == 0


def test_default_retention_is_60_days():
    assert config.TICKET_RETENTION_DAYS == 60
