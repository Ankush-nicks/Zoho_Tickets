"""
Zoho Creator "Instructors Ticketing System" CSV export -> ticket rows.

Shared by the Taxonomy tab's "Upload tickets CSV" (POST /api/tickets/
import-csv in main.py) and scripts/import_zoho_csv.py, so a ticket stored
from either path has the same raw_payload shape and timestamps.

plan_import() only decides what to write - it never touches the database
itself - so the same call backs both the upload's preview (dry run) and the
real apply (db.bulk_import_tickets).
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# CSV column name -> raw_payload key (snake_case, matching the live webhook's
# own field naming where an equivalent already exists). Columns not listed
# here are still kept, under a snake_cased version of their header (see
# _snake) - an export with extra columns shouldn't silently lose data.
FIELD_MAP = {
    "zoho_id": "record_id",
    "University": "university_boa",  # despite the key name, this is Zoho's "University" column - Zoho also has a separate, usually-empty "University BOA" field that this does NOT read from
    "Ticket ID": "zoho_ticket_id",
    "Ticket Status": "ticket_status",
    "Subject Name": "subject_name",
    "Assigned Team": "assigned_team",
    "Category Of The Issue": "category_of_the_issue",
    "Sub Category Of The Issue": "sub_category_of_the_issue",
    "Issue In Detail": "issue_in_detail",
    "Is it a recurring issue?": "is_it_a_recurring_issue",
    "What do you think is the best way to resolve the issue ASAP?": "resolution_preference",
    "Upload Supporting Files": "upload_supporting_files",
    "Assign Ticket To": "assign_ticket_to",
    "View Access": "view_access",
    "Transfer Ticket To": "transfer_ticket_to",
    "Ticket Raised By": "ticket_raised_by",
    "Added Time": "added_time",
    "Acknowledgement From The POC": "acknowledgement_from_the_poc",
    "Acknowledgement History": "acknowledgement_history",
    "WorkLog From The POC": "worklog_from_the_poc",
    "Worklog History": "worklog_history",
    "Resolution By The POC": "resolution_by_the_poc",
    "Ticket Closure Date-Time": "ticket_closure_date_time",
    "Ticket Closed By": "ticket_closed_by",
    "SLA Breach Status": "sla_breach_status",
    "Ticket Reopen_count": "ticket_reopen_count",
    "Last Reopened On": "last_reopened_on",
    "Ticket Transfer History": "ticket_transfer_history",
    "Last Transferred On": "last_transferred_on",
    "Last Transferred By": "last_transferred_by",
    "Session Section ID": "session_section_id",
    "Session ID": "session_id",
    "Session Type": "session_type",
    "Evaluation ID (QA Report ID)": "evaluation_id",
    "Added User": "added_user",
    "Modified Time": "modified_time",
    "Department Name": "department_name",
    "Instructor ID": "instructor_id",
    "Priority Level": "priority_level",
    "Campus City": "campus_city",
}

REQUIRED_COLUMNS = ("Ticket ID", "Issue In Detail")


def _snake(header: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", header.lower()).strip("_")


def parse_ist_timestamp(s: str | None) -> float | None:
    """'27/08/2026 16:10:13' (assumed IST, matching this org's timezone) -> UTC epoch."""
    if not s or not s.strip():
        return None
    dt = datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
    return dt.astimezone(timezone.utc).timestamp()


def build_raw_payload(row: dict) -> dict:
    return {
        FIELD_MAP.get(col) or _snake(col): (val or "").strip() or None
        for col, val in row.items()
        if col  # DictReader puts overflow cells of a ragged row under None
    }


def parse_csv(content: bytes) -> list[dict]:
    """
    Raw upload bytes -> list of row dicts. Accepts UTF-8 (with or without
    the BOM Excel adds) and falls back to Windows-1252 for files re-saved
    from Excel. Raises ValueError when a required column is missing, so the
    caller can reject the whole file before anything is written.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = headers
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(missing)}")
    return list(reader)


@dataclass
class ImportPlan:
    creates: list[dict] = field(default_factory=list)                 # db.create_ticket kwargs
    updates: list[tuple[str, dict]] = field(default_factory=list)     # (ticket id, changed fields)
    unchanged: int = 0
    newer_in_db: int = 0                                              # stored data newer than the CSV row
    skipped: list[dict] = field(default_factory=list)                 # {"line", "ticket_id", "reason"}


# Zoho's own test tickets - never real issues, so never imported.
TEST_CATEGORIES = {"dummy"}


def _parse_time_or_none(s: str | None) -> float | None:
    try:
        return parse_ist_timestamp(s)
    except ValueError:
        return None


def _stored_zoho_as_of(existing: dict) -> float | None:
    """
    When the Zoho data we already hold for a ticket was current. A CSV-
    sourced raw_payload carries Zoho's own modified_time; a webhook-sourced
    one doesn't (the webhook replaces raw_payload with the record as of that
    edit), so updated_at - when we stored it - stands in for it.
    """
    raw = existing.get("raw_payload") or {}
    return _parse_time_or_none(raw.get("modified_time")) or existing.get("updated_at")


def plan_import(rows: list[dict], existing_by_zoho_id: dict[str, dict]) -> ImportPlan:
    """
    Upserts by "Ticket ID":

    - New id: created as status='pending' with its real Zoho "Added Time" as
      created_at - the background auto-classify loop (or "Classify Now")
      classifies it later, with the instructor's category pick as a hint.
    - Known id: refreshes the Zoho-side data only - raw_payload (CSV columns
      overwrite, keys the CSV doesn't carry are kept), zoho_category/
      zoho_subcategory, and the issue text. Never touches category_id/
      status/confidence, so an upload can't undo a classification or a
      human correction - same rule as the webhook's edit branch. A category
      change here is NOT treated as a Zoho transfer (no auto-correction, no
      memory write): a bulk file can't tell a real transfer apart from our
      own write-back.
    - Rows with nothing different are counted as unchanged and not written.
    - Known id whose stored Zoho data is NEWER than the row's "Modified
      Time" (the live webhook already delivered a later edit than this
      export captured) is left alone and counted in newer_in_db - an export
      is a snapshot, and must never roll a ticket back to an older state.
    - Rows in a test category ("Dummy") are skipped.

    A Ticket ID repeated in the file: the last row wins.
    """
    plan = ImportPlan()
    latest: dict[str, tuple[int, dict]] = {}
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        zoho_ticket_id = (row.get("Ticket ID") or "").strip()
        issue_text = (row.get("Issue In Detail") or "").strip()
        if not zoho_ticket_id or not issue_text:
            plan.skipped.append({
                "line": line, "ticket_id": zoho_ticket_id or None,
                "reason": "missing Ticket ID" if not zoho_ticket_id else "missing Issue In Detail",
            })
            continue
        if (row.get("Category Of The Issue") or "").strip().lower() in TEST_CATEGORIES:
            plan.skipped.append({"line": line, "ticket_id": zoho_ticket_id, "reason": "test ticket (Dummy category)"})
            continue
        latest[zoho_ticket_id] = (line, row)

    for zoho_ticket_id, (line, row) in latest.items():
        issue_text = row["Issue In Detail"].strip()
        csv_raw = build_raw_payload(row)
        zoho_category = csv_raw.get("category_of_the_issue")
        zoho_subcategory = csv_raw.get("sub_category_of_the_issue")

        existing = existing_by_zoho_id.get(zoho_ticket_id)
        if existing is None:
            created_at = _parse_time_or_none(row.get("Added Time"))
            plan.creates.append({
                "original_text": issue_text,
                "zoho_ticket_id": zoho_ticket_id,
                "zoho_category": zoho_category,
                "zoho_subcategory": zoho_subcategory,
                "raw_payload": csv_raw,
                "created_at": created_at,
            })
            continue

        row_modified = _parse_time_or_none(row.get("Modified Time"))
        stored_as_of = _stored_zoho_as_of(existing)
        if row_modified is not None and stored_as_of is not None and stored_as_of > row_modified:
            plan.newer_in_db += 1
            continue

        changes: dict = {}
        merged_raw = {**(existing.get("raw_payload") or {}), **csv_raw}
        if merged_raw != (existing.get("raw_payload") or {}):
            changes["raw_payload"] = merged_raw
        if zoho_category != existing.get("zoho_category"):
            changes["zoho_category"] = zoho_category
        if zoho_subcategory != existing.get("zoho_subcategory"):
            changes["zoho_subcategory"] = zoho_subcategory
        if issue_text != existing.get("original_text"):
            changes["original_text"] = issue_text
            # full_context also carries any clarification answers - only
            # replace it when it's still just the original text.
            if existing.get("full_context") in (None, existing.get("original_text")):
                changes["full_context"] = issue_text
        if changes:
            plan.updates.append((existing["id"], changes))
        else:
            plan.unchanged += 1
    return plan
