import json
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

# Turso (via TURSO_DATABASE_URL) when set - so ticket history survives
# Render's ephemeral disk across spin-downs/redeploys. Falls back to local
# SQLite otherwise, unchanged from before local dev's perspective. Turso is
# a remote libSQL database that speaks the same SQL dialect as SQLite
# (including json_extract), and turso_serverless is a DB-API 2.0 driver with
# the same `?` paramstyle - so every query below runs unchanged against
# either backend; only the connection differs. Rows come back as plain
# tuples on both (turso_serverless's documented Row type isn't actually what
# fetchone()/fetchall() return in practice), so every read goes through
# _fetchone()/_fetchall() below, which zip each row against cursor.
# description to build a dict - never relying on a backend-specific row type.
USE_TURSO = bool(config.TURSO_DATABASE_URL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    original_text TEXT NOT NULL,
    full_context TEXT NOT NULL,       -- original text + appended Q&A, what we classify against
    status TEXT NOT NULL,             -- 'awaiting_clarification' | 'classified' | 'needs_human_review' | 'corrected'
    category_id TEXT,
    confidence REAL,
    reasoning TEXT,
    clarification_turns INTEGER NOT NULL DEFAULT 0,
    zoho_ticket_id TEXT,               -- set only for tickets sourced from a Zoho lookup, else NULL
    zoho_category TEXT,                -- category/sub-category Zoho already had on the record, if any -
    zoho_subcategory TEXT,             -- kept only to compare against our own prediction, never used to classify
    raw_payload TEXT,                  -- full JSON body Zoho's webhook sent (serialized - see _dump_raw_payload/_load_raw_payload)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolution_score REAL,             -- sum of the 5 weighted criteria below (out of 10), set by app/quality_scorer.py once a ticket is closed
    resolution_ack REAL,               -- Acknowledgement within 4 hours, max 2
    resolution_investigation REAL,     -- Investigation Done, max 1.5
    resolution_root_cause REAL,        -- Root Cause Fix, max 2.5
    resolution_sla REAL,               -- SLA, max 2
    resolution_detail REAL,            -- Resolution (detail), max 2
    resolution_evidence TEXT,          -- one-sentence AI critique quote, shown in the Insights tab
    resolution_scored_at REAL          -- unset means not graded yet (or not closed yet)
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    role TEXT NOT NULL,               -- 'system_question' | 'user_answer'
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    predicted_category_id TEXT,
    corrected_category_id TEXT NOT NULL,
    corrected_by TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);
"""


@contextmanager
def _conn():
    if USE_TURSO:
        import turso_serverless

        conn = turso_serverless.connect(config.TURSO_DATABASE_URL, auth_token=config.TURSO_AUTH_TOKEN)
    else:
        import sqlite3

        conn = sqlite3.connect(config.SQLITE_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _exec(conn, sql, params=()):
    """For INSERT/UPDATE/DELETE, or a query whose rows the caller doesn't
    need read back as dicts - use _fetchone()/_fetchall() for that instead."""
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def _row_to_dict(cur, row) -> dict:
    return {col[0]: val for col, val in zip(cur.description, row)}


def _fetchone(conn, sql, params=()) -> dict | None:
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    return _row_to_dict(cur, row) if row is not None else None


def _fetchall(conn, sql, params=()) -> list[dict]:
    cur = conn.cursor()
    cur.execute(sql, params)
    return [_row_to_dict(cur, row) for row in cur.fetchall()]


def _dump_raw_payload(raw_payload: dict | None) -> str | None:
    return json.dumps(raw_payload) if raw_payload is not None else None


def _load_raw_payload(row: dict) -> dict:
    """Mutates a row dict in place so raw_payload is a dict (or None) rather
    than the raw JSON TEXT column value."""
    raw = row.get("raw_payload")
    row["raw_payload"] = json.loads(raw) if raw else None
    return row


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def init_db():
    with _conn() as conn:
        for statement in SCHEMA.strip().split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)
        if not USE_TURSO:
            # Older local SQLite DB files predate these columns - add them if
            # missing (CREATE TABLE IF NOT EXISTS above is a no-op on an
            # existing table). Not needed on Turso, which always starts from
            # this same schema fresh.
            for col in ("zoho_ticket_id", "zoho_category", "zoho_subcategory", "raw_payload"):
                try:
                    conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} TEXT")
                except Exception:
                    pass
            for col in ("resolution_score", "resolution_ack", "resolution_investigation",
                        "resolution_root_cause", "resolution_sla", "resolution_detail", "resolution_scored_at"):
                try:
                    conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} REAL")
                except Exception:
                    pass
            try:
                conn.execute("ALTER TABLE tickets ADD COLUMN resolution_evidence TEXT")
            except Exception:
                pass


def create_ticket(
    original_text: str,
    zoho_ticket_id: str | None = None,
    zoho_category: str | None = None,
    zoho_subcategory: str | None = None,
    raw_payload: dict | None = None,
    created_at: float | None = None,
) -> str:
    """
    created_at defaults to now - only ever overridden by the historical CSV
    importer (scripts/import_zoho_csv.py), which needs each ticket's real
    Zoho creation time for correct day-bucketing/date-range filtering.
    update_ticket() always stamps updated_at to the current time (correct
    for real edits), so there's no equivalent override for that field.
    """
    ticket_id = new_id()
    now = created_at if created_at is not None else time.time()

    with _conn() as conn:
        _exec(
            conn,
            """INSERT INTO tickets
               (id, original_text, full_context, status, clarification_turns,
                zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)""",
            (ticket_id, original_text, original_text, zoho_ticket_id, zoho_category,
             zoho_subcategory, _dump_raw_payload(raw_payload), now, now),
        )
    _invalidate_list_all_cache()
    return ticket_id


def get_ticket(ticket_id: str) -> dict | None:
    with _conn() as conn:
        row = _fetchone(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return _load_raw_payload(row) if row else None


def get_ticket_by_zoho_id(zoho_ticket_id: str) -> dict | None:
    """
    Most recent ticket already stored for this Zoho ticket id, if any - lets
    the webhook upsert instead of creating a second row for the same Zoho
    ticket, whether the call is a retried "On Add" or a genuine "On Edit"
    (status change, POC acknowledgment, worklog, etc.).
    """
    with _conn() as conn:
        row = _fetchone(
            conn,
            "SELECT * FROM tickets WHERE zoho_ticket_id = ? ORDER BY created_at DESC LIMIT 1",
            (zoho_ticket_id,),
        )
        return _load_raw_payload(row) if row else None


def update_ticket(ticket_id: str, **fields):
    fields["updated_at"] = time.time()

    if "raw_payload" in fields:
        fields["raw_payload"] = _dump_raw_payload(fields["raw_payload"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [ticket_id]
    with _conn() as conn:
        _exec(conn, f"UPDATE tickets SET {cols} WHERE id = ?", vals)
    _invalidate_list_all_cache()


def append_turn(ticket_id: str, role: str, content: str):
    now = time.time()
    with _conn() as conn:
        _exec(
            conn,
            "INSERT INTO turns (id, ticket_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (new_id(), ticket_id, role, content, now),
        )


def get_turns(ticket_id: str) -> list[dict]:
    with _conn() as conn:
        return _fetchall(
            conn, "SELECT * FROM turns WHERE ticket_id = ? ORDER BY created_at ASC", (ticket_id,)
        )


def log_correction(ticket_id: str, predicted_category_id: str | None, corrected_category_id: str, corrected_by: str | None):
    now = time.time()
    with _conn() as conn:
        _exec(
            conn,
            """INSERT INTO corrections
               (id, ticket_id, predicted_category_id, corrected_category_id, corrected_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id(), ticket_id, predicted_category_id, corrected_category_id, corrected_by, now),
        )


_list_all_cache: dict = {"data": None, "at": 0.0}
_LIST_ALL_CACHE_TTL_SECONDS = 20


def _invalidate_list_all_cache():
    _list_all_cache["data"] = None


def list_all_tickets() -> list[dict]:
    """
    Every ticket ever stored, oldest first - backs the full CSV export, the
    Pulse/Insights dashboards, and the date-range views. Cached briefly and
    invalidated on every write since a single page load can fire several of
    these back-to-back. Prefer list_pending_tickets(), count_pending_
    tickets(), or list_tickets_by_raw_status() instead of this for anything
    that only needs a subset - those filter server-side on Turso too, so
    their read cost scales with the matching subset, not total history.
    """
    now = time.time()
    if _list_all_cache["data"] is not None and now - _list_all_cache["at"] < _LIST_ALL_CACHE_TTL_SECONDS:
        return _list_all_cache["data"]

    with _conn() as conn:
        rows = _fetchall(conn, "SELECT * FROM tickets ORDER BY created_at ASC")
        result = [_load_raw_payload(r) for r in rows]

    _list_all_cache["data"] = result
    _list_all_cache["at"] = now
    return result


def list_pending_tickets() -> list[dict]:
    """Tickets with status == 'pending' (e.g. from a --no-classify historical
    import), oldest first - a queue drained every 30 min regardless of order."""
    with _conn() as conn:
        rows = _fetchall(
            conn, "SELECT * FROM tickets WHERE status = 'pending' ORDER BY created_at ASC"
        )
        return [_load_raw_payload(r) for r in rows]


def count_pending_tickets() -> int:
    """Cheap count of status == 'pending' tickets - powers the "Classify Now" banner."""
    with _conn() as conn:
        row = _fetchone(conn, "SELECT COUNT(*) AS n FROM tickets WHERE status = 'pending'")
        return row["n"]


def list_tickets_by_raw_status(statuses: list[str]) -> list[dict]:
    """
    Tickets whose raw_payload.ticket_status (the Zoho status string) is one
    of `statuses` - used to find closed tickets for resolution grading
    without reading the entire ticket history. Still needs a Python pass
    afterward to check resolution_scored_at, since "field is absent" isn't
    something json_extract can filter on directly.
    """
    placeholders = ", ".join("?" for _ in statuses)
    with _conn() as conn:
        rows = _fetchall(
            conn,
            f"SELECT * FROM tickets WHERE json_extract(raw_payload, '$.ticket_status') IN ({placeholders})",
            statuses,
        )
        return [_load_raw_payload(r) for r in rows]


def list_tickets_for_date(date_str: str) -> list[dict]:
    """Tickets created on the given UTC calendar date (YYYY-MM-DD), newest first."""
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts = day_start.timestamp()
    end_ts = (day_start + timedelta(days=1)).timestamp()

    with _conn() as conn:
        rows = _fetchall(
            conn,
            "SELECT * FROM tickets WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC",
            (start_ts, end_ts),
        )
        return [_load_raw_payload(r) for r in rows]
