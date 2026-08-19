import sqlite3
import uuid
import json
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
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


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Older DB files predate the zoho_ticket_id column - add it if missing
        # (CREATE TABLE IF NOT EXISTS above is a no-op on an existing table).
        try:
            conn.execute("ALTER TABLE tickets ADD COLUMN zoho_ticket_id TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def create_ticket(original_text: str, zoho_ticket_id: str | None = None) -> str:
    ticket_id = new_id()
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tickets
               (id, original_text, full_context, status, clarification_turns, zoho_ticket_id, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (ticket_id, original_text, original_text, zoho_ticket_id, now, now),
        )
    return ticket_id


def get_ticket(ticket_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None


def update_ticket(ticket_id: str, **fields):
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [ticket_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE tickets SET {cols} WHERE id = ?", vals)


def append_turn(ticket_id: str, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO turns (id, ticket_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (new_id(), ticket_id, role, content, time.time()),
        )


def get_turns(ticket_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM turns WHERE ticket_id = ? ORDER BY created_at ASC", (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def log_correction(ticket_id: str, predicted_category_id: str | None, corrected_category_id: str, corrected_by: str | None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO corrections
               (id, ticket_id, predicted_category_id, corrected_category_id, corrected_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id(), ticket_id, predicted_category_id, corrected_category_id, corrected_by, time.time()),
        )


def stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
        by_status = conn.execute(
            "SELECT status, COUNT(*) c FROM tickets GROUP BY status"
        ).fetchall()
        corrections = conn.execute("SELECT COUNT(*) c FROM corrections").fetchone()["c"]
        by_category = conn.execute(
            "SELECT category_id, COUNT(*) c FROM tickets WHERE category_id IS NOT NULL GROUP BY category_id"
        ).fetchall()
        return {
            "total_tickets": total,
            "by_status": {r["status"]: r["c"] for r in by_status},
            "by_category": {r["category_id"]: r["c"] for r in by_category},
            "total_corrections": corrections,
        }
