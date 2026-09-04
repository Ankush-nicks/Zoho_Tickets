"""
Batch resolution-quality grading: runs app.quality_scorer.score_ticket()
against every closed ticket that hasn't been graded yet, same pipeline the
app's own background auto-score loop and "Score Now" button use.

Usage:
    OPENROUTER_API_KEY=sk-or-... python scripts/grade_resolutions.py [--limit N]

Respects whichever backend app.db is already configured for (TURSO_
DATABASE_URL set -> Turso, unset -> local SQLite) - run this with the same
environment variables you'd run the app itself with.

Safely re-runnable: only tickets missing resolution_scored_at are queried,
so an interrupted run (rate limit, crash) just picks up where it left off
next time - no separate results file or skip-list needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, quality_scorer  # noqa: E402
from openai import RateLimitError  # noqa: E402


def main():
    args = sys.argv[1:]
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    api_key = config.OPENROUTER_API_KEY
    if not api_key:
        print("Set OPENROUTER_API_KEY in the environment.")
        sys.exit(1)

    print(f"backend: {'Turso' if db.USE_TURSO else 'SQLite'}")
    db.init_db()

    closed = db.list_tickets_by_raw_status(list(quality_scorer.CLOSED_STATUSES))
    pending = [t for t in closed if not t.get("resolution_scored_at")]
    if limit is not None:
        pending = pending[:limit]
    print(f"{len(closed)} closed tickets, {len(pending)} ungraded - grading now\n")

    scored, failed = 0, 0
    for i, t in enumerate(pending, 1):
        try:
            result = quality_scorer.score_ticket(t, api_key)
        except RateLimitError as e:
            print(f"[{i}/{len(pending)}] RATE LIMITED - stopping here. Re-run this same command "
                  f"later (already-graded tickets are skipped automatically). {e}")
            break
        except Exception as e:
            print(f"[{i}/{len(pending)}] failed on {t.get('zoho_ticket_id') or t['id']}: {type(e).__name__}: {e}")
            failed += 1
            continue
        db.update_ticket(t["id"], **result)
        scored += 1
        if i % 25 == 0 or i == len(pending):
            print(f"[{i}/{len(pending)}] graded ({scored} ok, {failed} failed so far)")

    print(f"\nDone. scored={scored} failed={failed} remaining={len(pending) - scored - failed}")


if __name__ == "__main__":
    main()
