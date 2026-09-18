from app import db


def test_list_corrections_returns_predicted_and_corrected_ids(isolated_db):
    ticket_id = db.create_ticket("Some issue text", created_at=1000.0)

    db.log_correction(ticket_id, "cat-a", "cat-b", "poc@example.com")

    rows = db.list_corrections()
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == ticket_id
    assert rows[0]["predicted_category_id"] == "cat-a"
    assert rows[0]["corrected_category_id"] == "cat-b"
    assert rows[0]["corrected_by"] == "poc@example.com"


def test_list_corrections_allows_null_predicted_category(isolated_db):
    """A ticket can be corrected before it was ever successfully classified
    (e.g. still needs_human_review with no category_id) - predicted_category_id
    is nullable for exactly this case."""
    ticket_id = db.create_ticket("Some issue text", created_at=1000.0)

    db.log_correction(ticket_id, None, "cat-b", None)

    rows = db.list_corrections()
    assert rows[0]["predicted_category_id"] is None


def test_list_corrections_filters_by_date_range(isolated_db):
    from datetime import datetime, timezone

    ticket_id = db.create_ticket("Some issue text", created_at=1000.0)
    db.log_correction(ticket_id, "cat-a", "cat-b", None)

    row = db.list_corrections()[0]
    # Force the correction's created_at to a known UTC day so the range
    # filter has something deterministic to check against - log_correction
    # always stamps time.time(), which isn't controllable from here otherwise.
    day_ts = datetime.strptime("2026-01-15", "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    with db._conn() as conn:
        db._exec(conn, "UPDATE corrections SET created_at = ? WHERE id = ?", (day_ts, row["id"]))

    assert len(db.list_corrections(date_from="2026-01-15", date_to="2026-01-15")) == 1
    assert len(db.list_corrections(date_from="2026-01-16", date_to="2026-01-20")) == 0
    assert len(db.list_corrections(date_from="2026-01-10", date_to="2026-01-14")) == 0
