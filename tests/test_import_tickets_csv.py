"""
POST /api/tickets/import-csv - the Taxonomy tab's "Upload tickets CSV":
password gate, dry-run preview, upsert by Ticket ID, and never touching a
ticket's classification on update.
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app import main as main_module
from app.main import app

PASSWORD = "taxo-test-pw"
HEADER = ["Ticket ID", "Ticket Status", "Category Of The Issue", "Sub Category Of The Issue",
          "Issue In Detail", "Added Time", "Brand New Column"]


def _csv(rows, header=HEADER) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM, like Zoho/Excel exports


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "TAXONOMY_EDIT_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    # Run the import job inline so tests can assert right after the POST.
    monkeypatch.setattr(main_module, "_start_import_job", main_module._run_import_job)
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


def _upload(client, content, dry_run=False, password=PASSWORD):
    return client.post(
        f"/api/tickets/import-csv?dry_run={str(dry_run).lower()}",
        files={"file": ("tickets.csv", content, "text/csv")},
        headers={"X-Taxonomy-Password": password} if password else {},
    )


ROW_A = ["T-1", "Yet To Pick", "QA Report / Instructor Evaluation", "Feedback Too Generic or Vague",
         "Feedback is vague", "01/09/2026 10:00:00", "extra"]
ROW_B = ["T-2", "In Progress", "Facilities & Equipment", "Campus Network / Wi-Fi Issue",
         "Wifi down", "02/09/2026 11:30:00", ""]


def test_requires_taxonomy_password(client, isolated_db):
    assert _upload(client, _csv([ROW_A]), password=None).status_code == 403
    assert _upload(client, _csv([ROW_A]), password="wrong").status_code == 403
    assert isolated_db.get_ticket_by_zoho_id("T-1") is None


def test_requires_login(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "TAXONOMY_EDIT_PASSWORD", PASSWORD)
    assert _upload(TestClient(app), _csv([ROW_A])).status_code == 401


def test_dry_run_reports_counts_without_writing(client, isolated_db):
    body = _upload(client, _csv([ROW_A, ROW_B]), dry_run=True).json()
    assert (body["dry_run"], body["rows"], body["created"], body["updated"]) == (True, 2, 2, 0)
    assert isolated_db.get_ticket_by_zoho_id("T-1") is None


def test_creates_new_tickets_as_pending_with_all_columns(client, isolated_db):
    body = _upload(client, _csv([ROW_A, ROW_B])).json()
    assert body["created"] == 2

    t = isolated_db.get_ticket_by_zoho_id("T-1")
    assert t["status"] == "pending"
    assert t["original_text"] == "Feedback is vague"
    assert t["zoho_subcategory"] == "Feedback Too Generic or Vague"
    assert t["raw_payload"]["ticket_status"] == "Yet To Pick"
    assert t["raw_payload"]["brand_new_column"] == "extra"  # unmapped column kept, snake_cased
    # Backdated to Zoho's Added Time (10:00 IST = 04:30 UTC).
    assert t["created_at"] == pytest.approx(1788237000.0)


def test_reupload_of_same_file_is_unchanged(client):
    _upload(client, _csv([ROW_A, ROW_B]))
    body = _upload(client, _csv([ROW_A, ROW_B])).json()
    assert (body["created"], body["updated"], body["unchanged"]) == (0, 0, 2)


def test_update_refreshes_zoho_data_but_keeps_classification(client, isolated_db):
    ticket_id = isolated_db.create_ticket(
        "Feedback is vague", zoho_ticket_id="T-1",
        zoho_subcategory="Feedback Too Generic or Vague",
        raw_payload={"ticket_status": "Yet To Pick", "report_link": "https://x"},
    )
    isolated_db.update_ticket(ticket_id, status="corrected", category_id="G01-S01", confidence=1.0)

    changed = list(ROW_A)
    changed[1] = "Resolved By POC"
    changed[3] = "Instructor Scorecard Issues"
    body = _upload(client, _csv([changed])).json()
    assert (body["created"], body["updated"]) == (0, 1)

    t = isolated_db.get_ticket(ticket_id)
    assert t["raw_payload"]["ticket_status"] == "Resolved By POC"
    assert t["raw_payload"]["report_link"] == "https://x"  # key the CSV doesn't carry is kept
    assert t["zoho_subcategory"] == "Instructor Scorecard Issues"
    assert (t["status"], t["category_id"], t["confidence"]) == ("corrected", "G01-S01", 1.0)


def test_skips_incomplete_rows_and_last_duplicate_wins(client, isolated_db):
    no_id = [""] + ROW_A[1:]
    no_text = list(ROW_B)
    no_text[4] = ""
    dup = list(ROW_A)
    dup[4] = "Updated text"
    body = _upload(client, _csv([ROW_A, no_id, no_text, dup])).json()

    assert body["created"] == 1
    assert body["skipped_count"] == 2
    assert {s["reason"] for s in body["skipped"]} == {"missing Ticket ID", "missing Issue In Detail"}
    assert isolated_db.get_ticket_by_zoho_id("T-1")["original_text"] == "Updated text"


def test_rejects_csv_missing_required_columns(client):
    response = _upload(client, _csv([["T-1", "x"]], header=["Ticket ID", "Ticket Status"]))
    assert response.status_code == 400
    assert "Issue In Detail" in response.json()["detail"]


# --- safety rules added after auditing a real 3.5K-row export ----------------

MOD_HEADER = HEADER + ["Modified Time"]


def test_dummy_category_rows_are_skipped(client, isolated_db):
    dummy = ["T-9", "Discard", "Dummy", "Dummy", "test with dummy", "01/09/2026 10:00:00", ""]
    body = _upload(client, _csv([ROW_A, dummy])).json()
    assert body["created"] == 1
    assert body["skipped"] == [{"line": 3, "ticket_id": "T-9", "reason": "test ticket (Dummy category)"}]
    assert isolated_db.get_ticket_by_zoho_id("T-9") is None


def test_never_rolls_back_a_ticket_newer_in_db(client, isolated_db):
    # Webhook-sourced ticket (no modified_time in raw) stored AFTER the
    # export's Modified Time for it -> the export is stale for this ticket.
    ticket_id = isolated_db.create_ticket(
        "Feedback is vague", zoho_ticket_id="T-1", raw_payload={"ticket_status": "Resolved By POC"},
    )
    stale = ROW_A + ["01/01/2020 10:00:00"]
    body = _upload(client, _csv([stale], header=MOD_HEADER)).json()

    assert (body["updated"], body["newer_in_db"]) == (0, 1)
    assert isolated_db.get_ticket(ticket_id)["raw_payload"]["ticket_status"] == "Resolved By POC"


def test_updates_when_export_is_newer_than_stored_data(client, isolated_db):
    # CSV-sourced ticket: its own modified_time (not updated_at) says how
    # current it is.
    ticket_id = isolated_db.create_ticket(
        "Feedback is vague", zoho_ticket_id="T-1",
        raw_payload={"ticket_status": "Yet To Pick", "modified_time": "01/09/2026 10:00:00"},
    )
    newer = list(ROW_A)
    newer[1] = "Resolved By POC"
    body = _upload(client, _csv([newer + ["02/09/2026 10:00:00"]], header=MOD_HEADER)).json()

    assert (body["updated"], body["newer_in_db"]) == (1, 0)
    raw = isolated_db.get_ticket(ticket_id)["raw_payload"]
    assert raw["ticket_status"] == "Resolved By POC"
    assert raw["modified_time"] == "02/09/2026 10:00:00"


def test_status_endpoint_reports_finished_job(client):
    _upload(client, _csv([ROW_A, ROW_B]))
    job = client.get("/api/tickets/import-csv/status").json()
    assert job["state"] == "done"
    assert job["result"] == {"created": 2, "updated": 0, "already_existed": 0}
    assert job["done"] == job["total"] == 2


def test_second_import_while_one_runs_is_rejected(client, monkeypatch):
    assert main_module._import_lock.acquire(blocking=False)
    try:
        assert _upload(client, _csv([ROW_A])).status_code == 409
        # A preview never writes, so it's still allowed.
        assert _upload(client, _csv([ROW_A]), dry_run=True).status_code == 200
    finally:
        main_module._import_lock.release()


# --- db.bulk_import_tickets ----------------------------------------------------

def _create_op(zid):
    return {"original_text": f"text {zid}", "zoho_ticket_id": zid, "raw_payload": {"a": zid}}


def test_bulk_insert_never_duplicates_an_existing_zoho_id(isolated_db):
    isolated_db.create_ticket("already here", zoho_ticket_id="T-1")
    result = db.bulk_import_tickets([_create_op("T-1"), _create_op("T-2")], [])
    assert result == {"created": 1, "updated": 0, "already_existed": 1}
    assert len([t for t in db.list_all_tickets() if t["zoho_ticket_id"] == "T-1"]) == 1


def test_bulk_failure_keeps_committed_chunks_and_reports_progress(isolated_db):
    progress = []
    ops = [_create_op(f"T-{i}") for i in range(5)]
    ops[3]["original_text"] = None  # NOT NULL violation in the 2nd chunk
    with pytest.raises(Exception):
        db.bulk_import_tickets(ops, [], on_progress=lambda d, t: progress.append((d, t)), chunk_size=2)

    stored = {t["zoho_ticket_id"] for t in db.list_all_tickets()}
    assert stored == {"T-0", "T-1"}  # chunk 1 committed, chunk 2 rolled back whole
    assert progress == [(2, 5)]
    # Re-running finishes the rest without duplicating chunk 1.
    ops[3]["original_text"] = "fixed"
    result = db.bulk_import_tickets(ops, [], chunk_size=2)
    assert result == {"created": 3, "updated": 0, "already_existed": 2}
