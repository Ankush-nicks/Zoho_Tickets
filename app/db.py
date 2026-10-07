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
                                      -- | 'pending' | 'draft' (pre-submit row with no Zoho ticket yet - hidden everywhere)
    category_id TEXT,
    confidence REAL,
    reasoning TEXT,
    clarification_turns INTEGER NOT NULL DEFAULT 0,
    zoho_ticket_id TEXT,               -- set only for tickets sourced from a Zoho lookup, else NULL
    zoho_category TEXT,                -- category/sub-category Zoho already had on the record, if any -
    zoho_subcategory TEXT,             -- compared against our prediction, and fed to classify() as a reporter hint
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
    resolution_scored_at REAL,         -- unset means not graded yet (or not closed yet)
    acknowledged_at REAL,              -- set when a POC acknowledges a ticket in Zoho - nullable,
                                        -- unpopulated until a future write path exists - see
                                        -- docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md
    fallback_reason TEXT               -- set ONLY when the Zoho webhook had to force
                                        -- config.ZOHO_FALLBACK_CATEGORY_ID onto category_of_the_issue/
                                        -- sub_category_of_the_issue instead of a real classification
                                        -- (classify() itself failed, or category_id was orphaned by a
                                        -- later taxonomy edit) - NULL for a normal classification, including
                                        -- when the model itself legitimately picks the same catch-all leaf
    -- Routing log (see app/routing_log.py) - added by _migrate_columns() on
    -- databases that predate them:
    , reporter_category TEXT           -- what the instructor picked on the Zoho form, as sent,
    , reporter_subcategory TEXT        -- before our classifier overwrote those fields
    , reporter_leaf_id TEXT            -- reporter_subcategory resolved to a taxonomy leaf, if it did
    , model_category_id TEXT           -- the model's own pick and confidence, before the
    , model_confidence REAL            -- instructor's pick was weighed (classifier.apply_reporter_prior)
    , decision TEXT                    -- 'agreed' | 'kept' | 'overrode' | 'none' (no subcategory picked)
                                       -- - NULL for tickets classified before the routing log existed
    , duplicate_of TEXT                -- id of an earlier ticket with the same text from the same
                                       -- instructor within 7 days - counted once in accuracy
);

CREATE INDEX IF NOT EXISTS idx_tickets_status_created ON tickets(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_at);

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

-- Small shared JSON documents, versioned for optimistic concurrency - e.g.
-- the Daily Issue Check tab's groups/remarks (see put_shared_state).
CREATE TABLE IF NOT EXISTS shared_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT
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


# Columns added after the tickets table first shipped. CREATE TABLE IF NOT
# EXISTS is a no-op on an existing table - including the production Turso
# one - so these are added explicitly wherever they're missing.
_ADDED_COLUMNS = {
    "reporter_category": "TEXT",
    "reporter_subcategory": "TEXT",
    "reporter_leaf_id": "TEXT",
    "model_category_id": "TEXT",
    "model_confidence": "REAL",
    "decision": "TEXT",
    "duplicate_of": "TEXT",
}


def _migrate_columns(conn):
    have = {r["name"] for r in _fetchall(conn, "SELECT name FROM pragma_table_info('tickets')")}
    for col, kind in _ADDED_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {kind}")


def init_db():
    with _conn() as conn:
        # The tickets table must exist (and have the routing-log columns)
        # before the indexes in SCHEMA are created.
        statements = [s.strip() for s in SCHEMA.strip().split(";") if s.strip()]
        for statement in statements:
            if not statement.startswith("CREATE INDEX"):
                conn.execute(statement)
        _migrate_columns(conn)
        for statement in statements:
            if statement.startswith("CREATE INDEX"):
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
                        "resolution_root_cause", "resolution_sla", "resolution_detail", "resolution_scored_at",
                        "acknowledged_at"):
                try:
                    conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} REAL")
                except Exception:
                    pass
            try:
                conn.execute("ALTER TABLE tickets ADD COLUMN resolution_evidence TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE tickets ADD COLUMN fallback_reason TEXT")
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
    with _conn() as conn:
        ticket_id = _insert_ticket(
            conn, original_text, zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, created_at
        )
    _invalidate_list_all_cache()
    return ticket_id


def _insert_ticket(conn, original_text, zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, created_at,
                   status: str = "pending") -> str:
    ticket_id = new_id()
    now = created_at if created_at is not None else time.time()
    _exec(
        conn,
        """INSERT INTO tickets
           (id, original_text, full_context, status, clarification_turns,
            zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, created_at, updated_at)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, original_text, original_text, status, zoho_ticket_id, zoho_category,
         zoho_subcategory, _dump_raw_payload(raw_payload), now, now),
    )
    return ticket_id


# ---- drafts: the pre-submit call's row, before Zoho has a ticket id ----------

def create_draft(original_text: str, raw_payload: dict | None, **fields) -> str:
    """
    Stores the pre-submit classification as a 'draft' ticket (no Zoho
    ticket id yet) - see main.webhook_new_zoho_ticket. The On Add call that
    follows claims it (claim_draft); a draft that's never claimed (the
    instructor abandoned the form, or edited the text first) just stays a
    draft, hidden from every listing.
    """
    with _conn() as conn:
        ticket_id = _insert_ticket(conn, original_text, None, None, None, raw_payload, None, status="draft")
    if fields:
        update_ticket(ticket_id, **fields)
    return ticket_id


def claim_draft(original_text: str, since: float, **fields) -> dict | None:
    """
    The oldest unclaimed draft with exactly this text created at or after
    `since`, turned into a real ticket: `fields` (which must include a
    status other than 'draft' and the zoho_ticket_id) are written onto it.
    None when there's no such draft. Two On Add calls racing for the same
    draft can't both get it - the update only applies while the row is
    still a draft, and the winner is checked by reading it back.
    """
    assert fields.get("status") not in (None, "draft") and fields.get("zoho_ticket_id")
    fields["updated_at"] = time.time()
    if "raw_payload" in fields:
        fields["raw_payload"] = _dump_raw_payload(fields["raw_payload"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        candidates = _fetchall(
            conn,
            "SELECT id FROM tickets WHERE status = 'draft' AND created_at >= ? AND original_text = ? "
            "ORDER BY created_at ASC LIMIT 5",
            (since, original_text),
        )
        for c in candidates:
            _exec(conn, f"UPDATE tickets SET {cols} WHERE id = ? AND status = 'draft'", [*fields.values(), c["id"]])
            row = _fetchone(conn, "SELECT * FROM tickets WHERE id = ?", (c["id"],))
            if row and row.get("zoho_ticket_id") == fields["zoho_ticket_id"]:
                _invalidate_list_all_cache()
                return _load_raw_payload(row)
    return None


def list_tickets_since(since: float) -> list[dict]:
    """Every non-draft ticket created at or after `since` (epoch seconds),
    oldest first - indexed, so its cost scales with the window, not history."""
    with _conn() as conn:
        rows = _fetchall(
            conn,
            "SELECT * FROM tickets WHERE created_at >= ? AND status != 'draft' ORDER BY created_at ASC",
            (since,),
        )
        return [_load_raw_payload(r) for r in rows]


def count_drafts() -> int:
    """Pre-submit rows never claimed by an On Add call - roughly, how often
    instructors abandon a ticket (or edit its text) after the suggestion."""
    with _conn() as conn:
        return _fetchone(conn, "SELECT COUNT(*) AS n FROM tickets WHERE status = 'draft'")["n"]


BULK_IMPORT_CHUNK_SIZE = 25


def bulk_import_tickets(
    creates: list[dict],
    updates: list[tuple[str, dict]],
    on_progress=None,
    chunk_size: int = BULK_IMPORT_CHUNK_SIZE,
) -> dict:
    """
    Applies an app/zoho_csv.py ImportPlan (creates are create_ticket()
    kwargs, optionally plus status/category_id/confidence/reasoning - status
    defaults to 'pending'; updates are (ticket id, fields) pairs in
    update_ticket()'s shape) for the Taxonomy tab's CSV upload.

    Commits every `chunk_size` writes rather than all at once: on Turso each
    statement is its own HTTP round trip, so one transaction over thousands
    of rows would hold the database's write lock for minutes and stall the
    live Zoho webhook. A failure part-way keeps every chunk already
    committed (the failing chunk rolls back as a whole) - safe, since
    re-running the same upload only writes what's still missing/different.

    Inserts skip a zoho_ticket_id that already exists - the webhook can
    create the same ticket between planning and writing, and that must not
    become a duplicate row. Returns {"created", "updated", "already_existed"}.
    on_progress(done, total) is called after each committed chunk.
    """
    ops = [("create", c) for c in creates] + [("update", u) for u in updates]
    total = len(ops)
    counts = {"created": 0, "updated": 0, "already_existed": 0}
    now = time.time()
    try:
        for start in range(0, total, chunk_size):
            chunk_counts = {"created": 0, "updated": 0, "already_existed": 0}
            with _conn() as conn:
                for kind, op in ops[start:start + chunk_size]:
                    if kind == "create":
                        cur = _exec(
                            conn,
                            """INSERT INTO tickets
                               (id, original_text, full_context, status, clarification_turns,
                                category_id, confidence, reasoning,
                                zoho_ticket_id, zoho_category, zoho_subcategory, raw_payload, created_at, updated_at)
                               SELECT ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?
                               WHERE NOT EXISTS (SELECT 1 FROM tickets WHERE zoho_ticket_id = ?)""",
                            (new_id(), op["original_text"], op["original_text"], op.get("status") or "pending",
                             op.get("category_id"), op.get("confidence"), op.get("reasoning"),
                             op["zoho_ticket_id"], op.get("zoho_category"), op.get("zoho_subcategory"),
                             _dump_raw_payload(op.get("raw_payload")),
                             op.get("created_at") or now, op.get("created_at") or now,
                             op["zoho_ticket_id"]),
                        )
                        chunk_counts["created" if cur.rowcount == 1 else "already_existed"] += 1
                    else:
                        ticket_id, fields = op
                        fields = dict(fields, updated_at=now)
                        if "raw_payload" in fields:
                            fields["raw_payload"] = _dump_raw_payload(fields["raw_payload"])
                        cols = ", ".join(f"{k} = ?" for k in fields)
                        _exec(conn, f"UPDATE tickets SET {cols} WHERE id = ?", list(fields.values()) + [ticket_id])
                        chunk_counts["updated"] += 1
            # Only count a chunk once its commit (the _conn exit) succeeded.
            for k, v in chunk_counts.items():
                counts[k] += v
            if on_progress:
                on_progress(min(start + chunk_size, total), total)
    finally:
        _invalidate_list_all_cache()
    return counts


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


def list_corrections(date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """
    Every human correction ever logged (predicted_category_id -> corrected_
    category_id), oldest first, optionally restricted to [date_from, date_to]
    UTC calendar days (inclusive of both ends) - mirrors list_all_tickets()/
    _filter_tickets_by_date_range's contract so the client can do the same
    "fetch raw rows, aggregate in the browser" trick it already does for
    tickets (see /api/tickets/range) to build a confusion matrix and a
    correction-rate trend, without the server pre-computing either.

    This is the only place predicted_category_id survives after a
    correction - tickets.category_id gets overwritten with the corrected
    value, so the confusion matrix has to come from this table, not from
    the tickets table.
    """
    sql = "SELECT * FROM corrections"
    params: list = []
    clauses = []
    if date_from:
        clauses.append("created_at >= ?")
        params.append(datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    if date_to:
        clauses.append("created_at < ?")
        params.append((datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp())
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at ASC"
    with _conn() as conn:
        return _fetchall(conn, sql, params)


_list_all_cache: dict = {"data": None, "at": 0.0}
_LIST_ALL_CACHE_TTL_SECONDS = 20


def _invalidate_list_all_cache():
    _list_all_cache["data"] = None


def list_all_tickets() -> list[dict]:
    """
    Every ticket ever stored (drafts excluded), oldest first - backs the full CSV export, the
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
        rows = _fetchall(conn, "SELECT * FROM tickets WHERE status != 'draft' ORDER BY created_at ASC")
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
            f"SELECT * FROM tickets WHERE json_extract(raw_payload, '$.ticket_status') IN ({placeholders}) "
            "AND status != 'draft'",
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
            "SELECT * FROM tickets WHERE created_at >= ? AND created_at < ? AND status != 'draft' "
            "ORDER BY created_at DESC",
            (start_ts, end_ts),
        )
        return [_load_raw_payload(r) for r in rows]


def get_shared_state(key: str) -> dict:
    """{"value": <parsed JSON or None>, "version": int, "updated_at", "updated_by"} -
    version 0 / value None when nothing has been saved under `key` yet."""
    with _conn() as conn:
        row = _fetchone(conn, "SELECT * FROM shared_state WHERE key = ?", (key,))
    if not row:
        return {"value": None, "version": 0, "updated_at": None, "updated_by": None}
    return {"value": json.loads(row["value"]), "version": row["version"],
            "updated_at": row["updated_at"], "updated_by": row["updated_by"]}


def put_shared_state(key: str, value, expected_version: int, updated_by: str | None) -> tuple[bool, dict]:
    """
    Saves `value` only if the stored version is still `expected_version`
    (0 = nothing saved yet), as one conditional statement - two people
    saving at once can't silently overwrite each other; the loser gets
    (False, <current state>) and can reload it. Returns (True, <new state>)
    on success.
    """
    now = time.time()
    data = json.dumps(value, separators=(",", ":"))
    with _conn() as conn:
        if expected_version == 0:
            cur = _exec(
                conn,
                """INSERT INTO shared_state (key, value, version, updated_at, updated_by)
                   SELECT ?, ?, 1, ?, ? WHERE NOT EXISTS (SELECT 1 FROM shared_state WHERE key = ?)""",
                (key, data, now, updated_by, key),
            )
        else:
            cur = _exec(
                conn,
                """UPDATE shared_state SET value = ?, version = version + 1, updated_at = ?, updated_by = ?
                   WHERE key = ? AND version = ?""",
                (data, now, updated_by, key, expected_version),
            )
        ok = cur.rowcount == 1
    state = get_shared_state(key)
    return ok, state
