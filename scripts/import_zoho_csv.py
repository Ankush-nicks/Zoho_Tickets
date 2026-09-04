"""
One-off/reusable importer for a Zoho Creator "Instructors Ticketing System"
CSV export - classifies each row through the exact same pipeline a live
webhook ticket uses (app.classifier.classify), then stores it via app.db,
so imported history behaves identically to tickets that arrived normally
(shows up in Tickets/Stats, participates in the Zoho-tag agreement check,
etc.).

Usage:
    OPENROUTER_API_KEY=sk-or-... OPENAI_API_KEY=sk-... python scripts/import_zoho_csv.py "path/to/export.csv"

    OPENAI_API_KEY is only needed for few-shot embeddings - classification
    itself goes through OpenRouter.

Respects whichever backend app.db is already configured for (FIREBASE_
CREDENTIALS_BASE64 set -> Firestore, unset -> local SQLite) - run this with
the same environment variables you'd run the app itself with.

Safely re-runnable: rows whose "Ticket ID" already exists (via
db.get_ticket_by_zoho_id) are skipped rather than re-classified, so a
partial/failed run can just be started again.

Column mapping is specific to this export's header row - see FIELD_MAP
below if a differently-shaped CSV needs importing later.
"""
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import classifier, config, db  # noqa: E402
from openai import RateLimitError  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# CSV column name -> raw_payload key (snake_case, matching the live webhook's
# own field naming where an equivalent already exists).
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


def parse_ist_timestamp(s: str | None) -> float | None:
    """'27/08/2026 16:10:13' (assumed IST, matching this org's timezone) -> UTC epoch."""
    if not s or not s.strip():
        return None
    dt = datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
    return dt.astimezone(timezone.utc).timestamp()


def build_raw_payload(row: dict) -> dict:
    return {snake: (row.get(csv_col) or "").strip() or None for csv_col, snake in FIELD_MAP.items()}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_classify = "--no-classify" in sys.argv
    if len(args) < 1:
        print("Usage: python scripts/import_zoho_csv.py <path-to-csv> [--no-classify]")
        sys.exit(1)
    csv_path = Path(args[0])

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not no_classify and not api_key:
        print("Set OPENROUTER_API_KEY in the environment before running this (or pass --no-classify "
              "to import raw data only - use the portal's 'Classify Now' button to classify later).")
        sys.exit(1)

    print(f"backend: {'Firestore' if db.USE_FIRESTORE else 'SQLite'}, classify: {not no_classify}")
    db.init_db()

    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"{len(rows)} rows in {csv_path.name}")

    created, skipped, failed = 0, 0, 0
    for i, row in enumerate(rows, 1):
        zoho_ticket_id = (row.get("Ticket ID") or "").strip()
        issue_text = (row.get("Issue In Detail") or "").strip()
        if not zoho_ticket_id or not issue_text:
            print(f"[{i}/{len(rows)}] skip - missing Ticket ID or Issue In Detail")
            skipped += 1
            continue

        if db.get_ticket_by_zoho_id(zoho_ticket_id):
            print(f"[{i}/{len(rows)}] skip - {zoho_ticket_id} already imported")
            skipped += 1
            continue

        result = None
        if not no_classify:
            try:
                result = classifier.classify(issue_text, api_key)
            except RateLimitError as e:
                # Every remaining row would fail the same way - stop the
                # whole run rather than churning through them one by one
                # (this is what made the first version of this script
                # appear to hang for 9+ minutes on a rate-limited key).
                print(f"[{i}/{len(rows)}] RATE LIMITED - stopping here. Re-run this same command "
                      f"later (already-imported rows are skipped automatically) or pass "
                      f"--no-classify to import the rest unclassified. {e}")
                break
            except Exception as e:
                print(f"[{i}/{len(rows)}] FAILED classifying {zoho_ticket_id}: {e}")
                failed += 1
                continue

        created_at = parse_ist_timestamp(row.get("Added Time")) or datetime.now(timezone.utc).timestamp()

        zoho_category = (row.get("Category Of The Issue") or "").strip() or None
        zoho_subcategory = (row.get("Sub Category Of The Issue") or "").strip() or None
        raw_payload = build_raw_payload(row)

        ticket_id = db.create_ticket(
            issue_text,
            zoho_ticket_id=zoho_ticket_id,
            zoho_category=zoho_category,
            zoho_subcategory=zoho_subcategory,
            raw_payload=raw_payload,
            created_at=created_at,
        )
        # updated_at deliberately left to update_ticket's own time.time() -
        # it represents when this row was actually imported, which is the
        # only "update" this record has actually had in our system.
        if result is not None:
            # No live user to answer a clarifying question for historical
            # data - same fallback main.py uses once it's out of turns.
            status = "needs_human_review" if (result.needs_clarification or result.confidence < config.CONFIDENCE_THRESHOLD) else "classified"
            db.update_ticket(
                ticket_id,
                status=status,
                category_id=result.category_id,
                confidence=result.confidence,
                reasoning=result.reasoning,
            )
            created += 1
            print(f"[{i}/{len(rows)}] imported {zoho_ticket_id} -> {result.category_id} ({result.confidence:.2f}, {status})")
        else:
            # Left as status='pending' (create_ticket's default) - the
            # portal's "Classify Now" button (or the background auto-
            # classify loop) picks these up later.
            created += 1
            print(f"[{i}/{len(rows)}] imported {zoho_ticket_id} (unclassified)")

    print(f"\nDone. created={created} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
