import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

# Postgres (via DATABASE_URL, e.g. Neon/Supabase free tier) when set - so
# ticket history survives Render's ephemeral disk across spin-downs/redeploys.
# Falls back to local SQLite otherwise, unchanged from before.
USE_POSTGRES = bool(config.DATABASE_URL)

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    original_text TEXT NOT NULL,
    full_context TEXT NOT NULL,       -- original text + appended Q&A, what we classify against
    status TEXT NOT NULL,             -- 'awaiting_clarification' | 'classified' | 'needs_human_review' | 'corrected'
    category_id TEXT,
    subcategory TEXT,
    confidence REAL,
    reasoning TEXT,
    clarification_turns INTEGER NOT NULL DEFAULT 0,
    zoho_ticket_id TEXT,               -- set only for tickets sourced from a Zoho lookup, else NULL
    zoho_category TEXT,                -- category/sub-category Zoho already had on the record, if any -
    zoho_subcategory TEXT,             -- kept only to compare against our own prediction, never used to classify
    raw_payload TEXT,                  -- full JSON body Zoho's webhook sent, for analytics - unused elsewhere
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
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

# Same shape as SCHEMA_SQLITE, but timestamps as DOUBLE PRECISION - Postgres's
# REAL is single-precision (float4) and would truncate time.time()'s
# fractional-second epoch values that SQLite's REAL (a full double) does not.
SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    original_text TEXT NOT NULL,
    full_context TEXT NOT NULL,
    status TEXT NOT NULL,
    category_id TEXT,
    subcategory TEXT,
    confidence DOUBLE PRECISION,
    reasoning TEXT,
    clarification_turns INTEGER NOT NULL DEFAULT 0,
    zoho_ticket_id TEXT,
    zoho_category TEXT,
    zoho_subcategory TEXT,
    raw_payload TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    predicted_category_id TEXT,
    corrected_category_id TEXT NOT NULL,
    corrected_by TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
);
"""


def _placeholder(sql: str) -> str:
    return sql.replace("?", "%s") if USE_POSTGRES else sql


@contextmanager
def get_conn():
    if USE_POSTGRES:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        import sqlite3

        conn = sqlite3.connect(config.SQLITE_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _exec(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(_placeholder(sql), params)
    return cur


def init_db():
    with get_conn() as conn:
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute(SCHEMA_POSTGRES)
            # CREATE TABLE IF NOT EXISTS is a no-op against a table that
            # already exists, so a column added here after the table was
            # first created on a given database (e.g. zoho_category/
            # zoho_subcategory, added after some Postgres databases already
            # had a tickets table) never actually gets added there - hence
            # this explicit migration, safe to re-run every startup.
            for col in ("zoho_ticket_id", "zoho_category", "zoho_subcategory", "raw_payload"):
                cur.execute(f"ALTER TABLE tickets ADD COLUMN IF NOT EXISTS {col} TEXT")
        else:
            conn.executescript(SCHEMA_SQLITE)
            # Same idea for older SQLite DB files - SQLite lacks "ADD COLUMN
            # IF NOT EXISTS", hence the try/except instead.
            for col in ("zoho_ticket_id", "zoho_category", "zoho_subcategory", "raw_payload"):
                try:
                    conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} TEXT")
                except Exception:
                    pass


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def create_ticket(
    original_text: str,
    zoho_ticket_id: str | None = None,
    zoho_category: str | None = None,
    zoho_subcategory: str | None = None,
    raw_payload: str | None = None,
) -> str:
    ticket_id = new_id()
    now = time.time()
    with get_conn() as conn:
        _exec(
            conn,
            """INSERT INTO tickets
               (id, original_text, full_context, status, clarification_turns,
                zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)""",
            (ticket_id, original_text, original_text, zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, now, now),
        )
    return ticket_id


def get_ticket(ticket_id: str) -> dict | None:
    with get_conn() as conn:
        row = _exec(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None


def update_ticket(ticket_id: str, **fields):
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [ticket_id]
    with get_conn() as conn:
        _exec(conn, f"UPDATE tickets SET {cols} WHERE id = ?", vals)


def append_turn(ticket_id: str, role: str, content: str):
    with get_conn() as conn:
        _exec(
            conn,
            "INSERT INTO turns (id, ticket_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (new_id(), ticket_id, role, content, time.time()),
        )


def get_turns(ticket_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = _exec(
            conn, "SELECT * FROM turns WHERE ticket_id = ? ORDER BY created_at ASC", (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def log_correction(ticket_id: str, predicted_category_id: str | None, corrected_category_id: str, corrected_by: str | None):
    with get_conn() as conn:
        _exec(
            conn,
            """INSERT INTO corrections
               (id, ticket_id, predicted_category_id, corrected_category_id, corrected_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id(), ticket_id, predicted_category_id, corrected_category_id, corrected_by, time.time()),
        )


def list_all_tickets() -> list[dict]:
    """Every ticket ever stored, oldest first - backs the full CSV export."""
    with get_conn() as conn:
        rows = _exec(conn, "SELECT * FROM tickets ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]


def list_tickets_for_date(date_str: str) -> list[dict]:
    """
    Tickets created on the given UTC calendar date (YYYY-MM-DD), newest
    first. Bucketing is done in Python off a plain epoch-range query rather
    than SQL date functions, since those differ between SQLite and Postgres.
    """
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts = day_start.timestamp()
    end_ts = (day_start + timedelta(days=1)).timestamp()
    with get_conn() as conn:
        rows = _exec(
            conn,
            "SELECT * FROM tickets WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC",
            (start_ts, end_ts),
        ).fetchall()
        return [dict(r) for r in rows]


def list_ticket_dates() -> list[str]:
    """Distinct UTC calendar dates with at least one ticket, most recent first."""
    with get_conn() as conn:
        rows = _exec(conn, "SELECT created_at FROM tickets").fetchall()
    dates = {
        datetime.fromtimestamp(r["created_at"], tz=timezone.utc).strftime("%Y-%m-%d") for r in rows
    }
    return sorted(dates, reverse=True)


def stats() -> dict:
    with get_conn() as conn:
        total = _exec(conn, "SELECT COUNT(*) c FROM tickets").fetchone()["c"]
        by_status = _exec(conn, "SELECT status, COUNT(*) c FROM tickets GROUP BY status").fetchall()
        corrections = _exec(conn, "SELECT COUNT(*) c FROM corrections").fetchone()["c"]
        by_category = _exec(
            conn, "SELECT category_id, COUNT(*) c FROM tickets WHERE category_id IS NOT NULL GROUP BY category_id"
        ).fetchall()
        return {
            "total_tickets": total,
            "by_status": {r["status"]: r["c"] for r in by_status},
            "by_category": {r["category_id"]: r["c"] for r in by_category},
            "total_corrections": corrections,
        }
