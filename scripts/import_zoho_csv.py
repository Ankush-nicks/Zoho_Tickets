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

Respects whichever backend app.db is already configured for (TURSO_
DATABASE_URL set -> Turso, unset -> local SQLite) - run this with
the same environment variables you'd run the app itself with.

Safely re-runnable: rows whose "Ticket ID" already exists (via
db.get_ticket_by_zoho_id) are skipped rather than re-classified, so a
partial/failed run can just be started again.

Column mapping lives in app/zoho_csv.py's FIELD_MAP, shared with the
Taxonomy tab's "Upload tickets CSV" (which upserts without classifying).
"""
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import classifier, config, db  # noqa: E402
from app.main import _reporter_hint_from  # noqa: E402
from app.zoho_csv import build_raw_payload, parse_ist_timestamp  # noqa: E402
from openai import RateLimitError  # noqa: E402

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

    print(f"backend: {'Turso' if db.USE_TURSO else 'SQLite'}, classify: {not no_classify}")
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
                # Historical rows predate auto-fill, so the CSV's category
                # columns are the instructor's own pick - same hint the
                # live webhook uses.
                hint = _reporter_hint_from(
                    row.get("Category Of The Issue"), row.get("Sub Category Of The Issue")
                )
                result = classifier.classify(issue_text, api_key, reporter_hint=hint)
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
