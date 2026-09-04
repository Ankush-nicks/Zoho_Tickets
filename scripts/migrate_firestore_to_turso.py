"""
One-time migration: copies every ticket (+ its turns and corrections
subcollections) out of Firestore and into Turso, preserving ids, timestamps,
and every field exactly - not a re-import through create_ticket(), which
would generate new ids/timestamps and only accepts a subset of fields.

firebase-admin was removed from requirements.txt once the app itself
dropped Firestore support, so install it just for this one-off run:

    pip install firebase-admin

Usage:
    FIREBASE_CREDENTIALS_BASE64=... [FIREBASE_DATABASE_ID=...] \
    TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... \
    python scripts/migrate_firestore_to_turso.py

Safely re-runnable: every insert is `INSERT OR IGNORE` keyed on the
original Firestore document id, so an interrupted run can just be started
again without duplicating rows already copied over.

Does NOT delete or modify anything in Firestore - this only reads from it.
Once you've verified the Turso counts match (this script prints both) and
the app works against Turso, decommission the Firestore project/credentials
yourself whenever you're ready; nothing here does that automatically.
"""
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402


def _firestore_client():
    import firebase_admin
    from firebase_admin import credentials, firestore as admin_firestore

    creds_b64 = os.environ.get("FIREBASE_CREDENTIALS_BASE64")
    if not creds_b64:
        print("Set FIREBASE_CREDENTIALS_BASE64 in the environment.")
        sys.exit(1)
    database_id = os.environ.get("FIREBASE_DATABASE_ID", "(default)")

    cred_info = json.loads(base64.b64decode(creds_b64))
    cred = credentials.Certificate(cred_info)
    try:
        app = firebase_admin.get_app()
    except ValueError:
        app = firebase_admin.initialize_app(cred)
    return admin_firestore.client(app=app, database_id=database_id)


def _doc_to_dict(snap) -> dict:
    d = snap.to_dict() or {}
    d["id"] = snap.id
    return d


TICKET_COLUMNS = [
    "id", "original_text", "full_context", "status", "category_id", "confidence",
    "reasoning", "clarification_turns", "zoho_ticket_id", "zoho_category",
    "zoho_subcategory", "raw_payload", "created_at", "updated_at",
    "resolution_score", "resolution_ack", "resolution_investigation",
    "resolution_root_cause", "resolution_sla", "resolution_detail",
    "resolution_evidence", "resolution_scored_at",
]


def _migrate_ticket(conn, fs_client, ticket_doc) -> None:
    t = _doc_to_dict(ticket_doc)
    values = [
        t.get(col) if col != "raw_payload" else db._dump_raw_payload(t.get("raw_payload"))
        for col in TICKET_COLUMNS
    ]
    placeholders = ", ".join("?" for _ in TICKET_COLUMNS)
    db._exec(
        conn,
        f"INSERT OR IGNORE INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES ({placeholders})",
        values,
    )

    for turn_doc in ticket_doc.reference.collection("turns").order_by("created_at").stream():
        turn = _doc_to_dict(turn_doc)
        db._exec(
            conn,
            "INSERT OR IGNORE INTO turns (id, ticket_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (turn["id"], t["id"], turn.get("role"), turn.get("content"), turn.get("created_at")),
        )

    for corr_doc in ticket_doc.reference.collection("corrections").stream():
        corr = _doc_to_dict(corr_doc)
        db._exec(
            conn,
            """INSERT OR IGNORE INTO corrections
               (id, ticket_id, predicted_category_id, corrected_category_id, corrected_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (corr["id"], t["id"], corr.get("predicted_category_id"), corr.get("corrected_category_id"),
             corr.get("corrected_by"), corr.get("created_at")),
        )


def main():
    if not config.TURSO_DATABASE_URL:
        print("Set TURSO_DATABASE_URL (and TURSO_AUTH_TOKEN) in the environment.")
        sys.exit(1)

    fs_client = _firestore_client()
    db.init_db()  # creates the schema in Turso if this is a fresh database

    docs = list(fs_client.collection("tickets").stream())
    print(f"{len(docs)} tickets found in Firestore")

    with db._conn() as conn:
        for i, ticket_doc in enumerate(docs, 1):
            _migrate_ticket(conn, fs_client, ticket_doc)
            if i % 50 == 0 or i == len(docs):
                print(f"[{i}/{len(docs)}] migrated")

    db._invalidate_list_all_cache()
    turso_count = len(db.list_all_tickets())
    print(f"\nFirestore tickets: {len(docs)}")
    print(f"Turso tickets after migration: {turso_count}")
    if turso_count < len(docs):
        print("Counts don't match - re-run this script (safe/idempotent) or investigate before cutting over.")
    else:
        print("Counts match. Verify the app works against Turso before removing FIREBASE_* env vars.")


if __name__ == "__main__":
    main()
