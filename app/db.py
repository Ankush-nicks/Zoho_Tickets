import base64
import json
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

# Firestore (via FIREBASE_CREDENTIALS_BASE64) when set - so ticket history
# survives Render's ephemeral disk across spin-downs/redeploys. Falls back to
# local SQLite otherwise, unchanged from before local dev's perspective.
USE_FIRESTORE = bool(config.FIREBASE_CREDENTIALS_BASE64)

SCHEMA_SQLITE = """
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


# --- Firestore client -------------------------------------------------------
# Lazily initialized (only ever needed when USE_FIRESTORE is true) so local
# dev never has to import firebase_admin or hold live credentials.

_fs_client_singleton = None


def _fs_client():
    global _fs_client_singleton
    if _fs_client_singleton is not None:
        return _fs_client_singleton

    import firebase_admin
    from firebase_admin import credentials, firestore as admin_firestore

    cred_info = json.loads(base64.b64decode(config.FIREBASE_CREDENTIALS_BASE64))
    cred = credentials.Certificate(cred_info)
    try:
        app = firebase_admin.get_app()
    except ValueError:
        app = firebase_admin.initialize_app(cred)
    _fs_client_singleton = admin_firestore.client(app=app, database_id=config.FIREBASE_DATABASE_ID)
    return _fs_client_singleton


def _doc_to_dict(snap) -> dict:
    d = snap.to_dict() or {}
    d["id"] = snap.id
    return d


# --- SQLite (local dev) helpers ---------------------------------------------


def _placeholder(sql: str) -> str:
    return sql


@contextmanager
def _sqlite_conn():
    import sqlite3

    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _sqlite_exec(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def _dump_raw_payload(raw_payload: dict | None) -> str | None:
    return json.dumps(raw_payload) if raw_payload is not None else None


def _load_raw_payload(row: dict) -> dict:
    """Mutates a SQLite row dict in place so raw_payload is a dict (or None),
    matching what Firestore already returns natively - callers never need to
    know which backend served the row."""
    raw = row.get("raw_payload")
    row["raw_payload"] = json.loads(raw) if raw else None
    return row


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def init_db():
    if USE_FIRESTORE:
        _fs_client()  # fail fast at startup if credentials/database are misconfigured
        return
    with _sqlite_conn() as conn:
        conn.executescript(SCHEMA_SQLITE)
        # Older SQLite DB files predate these columns - add them if missing
        # (CREATE TABLE IF NOT EXISTS above is a no-op on an existing table).
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

    if USE_FIRESTORE:
        _fs_client().collection("tickets").document(ticket_id).set({
            "original_text": original_text,
            "full_context": original_text,
            "status": "pending",
            "category_id": None,
            "confidence": None,
            "reasoning": None,
            "clarification_turns": 0,
            "zoho_ticket_id": zoho_ticket_id,
            "zoho_category": zoho_category,
            "zoho_subcategory": zoho_subcategory,
            "raw_payload": raw_payload,
            "created_at": now,
            "updated_at": now,
        })
        return ticket_id

    with _sqlite_conn() as conn:
        _sqlite_exec(
            conn,
            """INSERT INTO tickets
               (id, original_text, full_context, status, clarification_turns,
                zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)""",
            (ticket_id, original_text, original_text, zoho_ticket_id, zoho_category,
             zoho_subcategory, _dump_raw_payload(raw_payload), now, now),
        )
    return ticket_id


def get_ticket(ticket_id: str) -> dict | None:
    if USE_FIRESTORE:
        snap = _fs_client().collection("tickets").document(ticket_id).get()
        return _doc_to_dict(snap) if snap.exists else None

    with _sqlite_conn() as conn:
        row = _sqlite_exec(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _load_raw_payload(dict(row)) if row else None


def get_ticket_by_zoho_id(zoho_ticket_id: str) -> dict | None:
    """
    Most recent ticket already stored for this Zoho ticket id, if any - lets
    the webhook upsert instead of creating a second row for the same Zoho
    ticket, whether the call is a retried "On Add" or a genuine "On Edit"
    (status change, POC acknowledgment, worklog, etc.).
    """
    if USE_FIRESTORE:
        from google.cloud.firestore_v1 import FieldFilter

        # Fetch all matches and pick the newest in Python rather than
        # .order_by('created_at') server-side - filtering on one field and
        # ordering by another needs a composite index, and there are only
        # ever a handful of docs per zoho_ticket_id.
        docs = list(_fs_client().collection("tickets")
                    .where(filter=FieldFilter("zoho_ticket_id", "==", zoho_ticket_id)).stream())
        if not docs:
            return None
        best = max(docs, key=lambda s: s.to_dict().get("created_at", 0))
        return _doc_to_dict(best)

    with _sqlite_conn() as conn:
        row = _sqlite_exec(
            conn,
            "SELECT * FROM tickets WHERE zoho_ticket_id = ? ORDER BY created_at DESC LIMIT 1",
            (zoho_ticket_id,),
        ).fetchone()
        return _load_raw_payload(dict(row)) if row else None


def update_ticket(ticket_id: str, **fields):
    fields["updated_at"] = time.time()

    if USE_FIRESTORE:
        _fs_client().collection("tickets").document(ticket_id).update(fields)
        return

    if "raw_payload" in fields:
        fields["raw_payload"] = _dump_raw_payload(fields["raw_payload"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [ticket_id]
    with _sqlite_conn() as conn:
        _sqlite_exec(conn, f"UPDATE tickets SET {cols} WHERE id = ?", vals)


def append_turn(ticket_id: str, role: str, content: str):
    now = time.time()
    if USE_FIRESTORE:
        turn_id = new_id()
        (_fs_client().collection("tickets").document(ticket_id)
         .collection("turns").document(turn_id)
         .set({"role": role, "content": content, "created_at": now}))
        return

    with _sqlite_conn() as conn:
        _sqlite_exec(
            conn,
            "INSERT INTO turns (id, ticket_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (new_id(), ticket_id, role, content, now),
        )


def get_turns(ticket_id: str) -> list[dict]:
    if USE_FIRESTORE:
        docs = (_fs_client().collection("tickets").document(ticket_id)
                .collection("turns").order_by("created_at").stream())
        return [_doc_to_dict(d) for d in docs]

    with _sqlite_conn() as conn:
        rows = _sqlite_exec(
            conn, "SELECT * FROM turns WHERE ticket_id = ? ORDER BY created_at ASC", (ticket_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def log_correction(ticket_id: str, predicted_category_id: str | None, corrected_category_id: str, corrected_by: str | None):
    now = time.time()
    if USE_FIRESTORE:
        correction_id = new_id()
        (_fs_client().collection("tickets").document(ticket_id)
         .collection("corrections").document(correction_id)
         .set({
             "predicted_category_id": predicted_category_id,
             "corrected_category_id": corrected_category_id,
             "corrected_by": corrected_by,
             "created_at": now,
         }))
        return

    with _sqlite_conn() as conn:
        _sqlite_exec(
            conn,
            """INSERT INTO corrections
               (id, ticket_id, predicted_category_id, corrected_category_id, corrected_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_id(), ticket_id, predicted_category_id, corrected_category_id, corrected_by, now),
        )


def list_all_tickets() -> list[dict]:
    """Every ticket ever stored, oldest first - backs the full CSV export."""
    if USE_FIRESTORE:
        docs = _fs_client().collection("tickets").order_by("created_at").stream()
        return [_doc_to_dict(d) for d in docs]

    with _sqlite_conn() as conn:
        rows = _sqlite_exec(conn, "SELECT * FROM tickets ORDER BY created_at ASC").fetchall()
        return [_load_raw_payload(dict(r)) for r in rows]


def list_tickets_for_date(date_str: str) -> list[dict]:
    """
    Tickets created on the given UTC calendar date (YYYY-MM-DD), newest
    first. Bucketing is done off a plain epoch-range query rather than SQL/
    Firestore date functions, since those differ between backends.
    """
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts = day_start.timestamp()
    end_ts = (day_start + timedelta(days=1)).timestamp()

    if USE_FIRESTORE:
        from google.cloud.firestore_v1 import FieldFilter

        docs = (_fs_client().collection("tickets")
                .where(filter=FieldFilter("created_at", ">=", start_ts))
                .where(filter=FieldFilter("created_at", "<", end_ts))
                .order_by("created_at", direction="DESCENDING")
                .stream())
        return [_doc_to_dict(d) for d in docs]

    with _sqlite_conn() as conn:
        rows = _sqlite_exec(
            conn,
            "SELECT * FROM tickets WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC",
            (start_ts, end_ts),
        ).fetchall()
        return [_load_raw_payload(dict(r)) for r in rows]
