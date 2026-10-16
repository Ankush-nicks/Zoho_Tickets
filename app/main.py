import asyncio
import csv
import io
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import logging

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from openai import RateLimitError
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, memory, classifier, zoho_csv, routing_log, ticket_events
from .taxonomy import taxonomy
from .auth import require_login
from .models import (
    LoginRequest,
    TaxonomyUnlockRequest,
    ClassificationResult,
)

app = FastAPI(title="Ticket Classifier", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="ticket_router_session")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

logger = logging.getLogger("uvicorn.error")


@app.exception_handler(RequestValidationError)
async def zoho_webhook_debug_handler(request: Request, exc: RequestValidationError):
    """
    TEMPORARY DIAGNOSTIC (added to debug the live Zoho integration - safe to
    remove once confirmed working). Logs the raw body Zoho actually sent
    whenever a webhook payload fails validation, visible in Render's
    Application logs, so we can see exactly what Zoho is sending without
    needing direct access to the Zoho side.
    """
    if request.url.path == "/api/webhooks/zoho/tickets":
        try:
            raw_body = await request.body()
        except Exception as e:
            raw_body = f"<could not read body: {e}>".encode()
        logger.error(
            "ZOHO WEBHOOK DEBUG - validation failed. errors=%s raw_body=%r",
            exc.errors(), raw_body,
        )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def require_webhook_secret(x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret")) -> None:
    """
    Authenticates Zoho Creator's Deluge "On Add" workflow for the push
    endpoint below - deliberately NOT the session-cookie login the UI uses,
    since a Deluge script can't practically hold a browser session. Fails
    closed: an unset ZOHO_WEBHOOK_SECRET refuses every request rather than
    silently accepting an unauthenticated one.
    """
    if not config.ZOHO_WEBHOOK_SECRET or not x_webhook_secret or not secrets.compare_digest(
        x_webhook_secret, config.ZOHO_WEBHOOK_SECRET
    ):
        raise HTTPException(401, "Missing or invalid X-Webhook-Secret header.")


@app.on_event("startup")
async def startup():
    db.init_db()
    taxonomy.load_live()
    # Bring the vector memory's taxonomy examples in line with taxonomy.json
    # (a deploy may have changed them) - see _sync_seed_examples.
    asyncio.create_task(asyncio.to_thread(_sync_seed_examples, "startup"))
    asyncio.create_task(_settle_pending_csv_imports_at_startup())
    asyncio.create_task(_auto_classify_loop())


async def _settle_pending_csv_imports_at_startup():
    # Off the event loop: on Turso this is one round trip per ticket, and an
    # earlier upload can have left thousands pending.
    try:
        await asyncio.to_thread(_settle_pending_csv_imports)
    except Exception as e:
        logger.error("startup settle of pending CSV imports failed: %s", e)


# App pages - all served by index(). Also the only places login will
# send you back to, so ?next= can't be used as an open redirect.
PAGE_PATHS = ("/log", "/classify", "/taxonomy")


def _login_redirect(next_path: str) -> RedirectResponse:
    if next_path in PAGE_PATHS:
        return RedirectResponse(f"/login?next={next_path}")
    return RedirectResponse("/login")


@app.get("/")
@app.get("/taxonomy")
@app.get("/classify")
@app.get("/log")
def index(request: Request):
    """Every page is the same shell (one shared header); index.html
    reads the path to decide which page to show."""
    if not request.session.get("user"):
        return _login_redirect(request.url.path)
    return FileResponse(str(STATIC_DIR / "index.html"))


# Pages that were removed (Daily report, Pulse, Stats, Drill down) - keep
# old links and bookmarks working by sending them to the Log.
@app.get("/daily")
@app.get("/pulse")
@app.get("/stats")
@app.get("/drill-down")
@app.get("/tickets")
@app.get("/daily-issue")
@app.get("/daily-issue-check")
def removed_page():
    return RedirectResponse("/log")


@app.get("/login")
def login_page(request: Request, next: str = ""):
    if request.session.get("user"):
        return RedirectResponse(next if next in PAGE_PATHS else "/log")
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    if not auth.verify_credentials(req.username, req.password):
        raise HTTPException(401, "Invalid username or password.")
    request.session["user"] = req.username
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/taxonomy")
def get_taxonomy(user: str = Depends(require_login)):
    return {"version": taxonomy.version, "categories": taxonomy.groups}


@app.post("/api/taxonomy/unlock")
def unlock_taxonomy(req: TaxonomyUnlockRequest, user: str = Depends(require_login)):
    """
    Second gate in front of the Taxonomy tab's editor - separate from the
    app-wide login. Checked here for immediate "Unlock to edit" UI feedback;
    checked again on every PUT /api/taxonomy below since this endpoint alone
    can't stop someone from calling the save endpoint directly.
    """
    if not secrets.compare_digest(req.password, config.TAXONOMY_EDIT_PASSWORD):
        raise HTTPException(403, "Incorrect password.")
    return {"ok": True}


def _require_taxonomy_password(x_taxonomy_password: str | None) -> None:
    if not x_taxonomy_password or not secrets.compare_digest(
        x_taxonomy_password, config.TAXONOMY_EDIT_PASSWORD
    ):
        raise HTTPException(403, "Taxonomy editing is locked - incorrect or missing password.")


@app.put("/api/taxonomy")
def update_taxonomy(
    payload: dict,
    user: str = Depends(require_login),
    x_taxonomy_password: str | None = Header(None),
):
    """
    Full-replace save for the Taxonomy tab's editor - payload is the same
    {version, categories: [...]} shape GET /api/taxonomy returns, since the
    editor works off one in-browser copy and saves the whole thing back
    rather than patching individual fields. Rejects structurally invalid
    data (missing ids/names, duplicate ids) with a 400 before writing
    anything; a valid payload is written to taxonomy.json and hot-reloaded,
    so classify() sees it immediately, no restart needed.

    Requires the taxonomy-edit password on every call (X-Taxonomy-Password
    header), not just at "Unlock to edit" time - the browser holds it in
    memory only after a successful unlock and resends it here, so a request
    made straight against the API without the password is rejected too.
    """
    _require_taxonomy_password(x_taxonomy_password)
    try:
        with _taxonomy_edit_lock:
            taxonomy.save(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # The classifier reads definitions straight from taxonomy.json, but its
    # few-shot examples come from the vector memory - refresh those too.
    return {"version": taxonomy.version, "categories": taxonomy.groups,
            "examples_sync": _start_seed_sync("taxonomy save")}


# One sync at a time: each run re-reads the current taxonomy, so a save made
# while another sync is running is picked up by the next run, and two runs
# can never interleave their delete/add of the seed entries.
_seed_sync_lock = threading.Lock()

# Held across every read -> change -> save of the taxonomy, so two edits made
# at once (e.g. two corrections, or a correction and the editor's save) can't
# both start from the same snapshot and lose one another's change.
_taxonomy_edit_lock = threading.Lock()


def _sync_seed_examples(reason: str) -> bool:
    """
    Makes the vector memory's seed examples (the taxonomy's examples, which
    the classifier retrieves as few-shot context) match taxonomy.json:
    replaces them via memory.reseed() - every learned correction stays -
    and skips the embeddings call when they already match. Never raises.
    Returns True when memory matches the taxonomy afterwards, False when it
    couldn't be updated.
    """
    if not config.OPENAI_API_KEY:
        logger.warning("taxonomy examples not synced to memory (%s): OPENAI_API_KEY is not set", reason)
        return False
    with _seed_sync_lock:
        try:
            seeds = taxonomy.seed_examples()
            if memory.seeds_match(seeds):
                return True
            removed, added = memory.reseed(seeds, config.OPENAI_API_KEY)
            logger.info("taxonomy examples synced to memory (%s): removed %d, added %d", reason, removed, added)
            return True
        except Exception as e:
            logger.error(f"syncing taxonomy examples to memory failed ({reason}): {e}")
            return False


def _start_seed_sync(reason: str) -> str:
    """Runs _sync_seed_examples in the background (a reseed embeds every
    example - too slow to hold the save request open). Returns 'started',
    or 'skipped' when there's no OPENAI_API_KEY to embed with."""
    if not config.OPENAI_API_KEY:
        logger.warning("taxonomy examples not synced to memory (%s): OPENAI_API_KEY is not set", reason)
        return "skipped"
    threading.Thread(target=_sync_seed_examples, args=(reason,), daemon=True).start()
    return "started"


# ---------------------------------------------------------------------------
# Classify page: suggest a category for pasted text, learn from corrections.
# ---------------------------------------------------------------------------

def _label(leaf_id: str) -> dict:
    """{category_id, category, subcategory} for a leaf id (names fall back
    to the id when it is no longer in the taxonomy)."""
    leaf = taxonomy.get(leaf_id) or {}
    return {
        "category_id": leaf_id,
        "category": leaf.get("parent_name") or "",
        "subcategory": leaf.get("name") or leaf_id,
    }


class ClassifyRequest(BaseModel):
    text: str


class ClassifyCorrectRequest(BaseModel):
    text: str
    category_id: str


def _classify_text(text: str):
    if not config.OPENROUTER_API_KEY:
        raise HTTPException(503, "Classification isn't available: OPENROUTER_API_KEY is not set on the server.")
    try:
        return classifier.classify(text, config.OPENROUTER_API_KEY)
    except Exception as e:
        raise HTTPException(502, f"Couldn't get a suggestion: {str(e)[:300]}")


@app.post("/api/classify")
def classify_text(req: ClassifyRequest, user: str = Depends(require_login)):
    """Suggests a category for the text. Writes nothing."""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Enter the issue text first.")
    result = _classify_text(text)
    return {**_label(result.category_id), "confidence": result.confidence, "reasoning": result.reasoning}


@app.post("/api/classify/correct")
def classify_correct(
    req: ClassifyCorrectRequest,
    user: str = Depends(require_login),
    x_taxonomy_password: str | None = Header(None),
):
    """Adds the text as an example of the chosen subcategory (unless it is
    already one), syncs memory, then re-runs the classifier on the text."""
    _require_taxonomy_password(x_taxonomy_password)
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Enter the issue text first.")
    if not taxonomy.get(req.category_id):
        raise HTTPException(400, f"Unknown category_id: {req.category_id}")
    if not config.OPENROUTER_API_KEY:
        raise HTTPException(503, "Classification isn't available: OPENROUTER_API_KEY is not set on the server.")

    def norm(t: str) -> str:
        return " ".join(t.lower().split())

    with _taxonomy_edit_lock:
        data = taxonomy.as_dict()
        sub = next(s for g in data["categories"] for s in g.get("subcategories", []) if s["id"] == req.category_id)
        examples = sub.setdefault("examples", [])
        added = norm(text) not in {norm(e) for e in examples}
        if added:
            examples.append(" ".join(text.split()))
            try:
                taxonomy.save(data)
            except ValueError as e:
                raise HTTPException(400, str(e))
    memory_synced = _sync_seed_examples("classify correction")
    out = {"added": added, "memory_synced": memory_synced}
    try:
        result = classifier.classify(text, config.OPENROUTER_API_KEY)
    except Exception as e:
        # The correction is already saved - report it, not a failure.
        return {**out, "recheck": None, "recheck_error": str(e)[:300]}
    out["recheck"] = {**_label(result.category_id), "confidence": result.confidence,
                      "correct": result.category_id == req.category_id}
    return out


@app.get("/api/taxonomy/export.csv")
def export_taxonomy_csv(user: str = Depends(require_login)):
    """The whole taxonomy, one row per subcategory, every field included.
    Each example gets its own column (example_1, example_2, ...) - some
    examples contain line breaks, so they can't share one cell."""
    subs = [(g, s) for g in taxonomy.groups for s in g.get("subcategories", [])]
    max_examples = max((len(s.get("examples", [])) for _, s in subs), default=0)
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM, so Excel reads the examples' non-ASCII text as UTF-8
    writer = csv.writer(buf)
    writer.writerow([
        "category_id", "category_name",
        "subcategory_id", "subcategory_name", "description",
        "assigned_team", "poc_primary", "example_count",
        *(f"example_{i}" for i in range(1, max_examples + 1)),
    ])
    for group, sub in subs:
        examples = sub.get("examples", [])
        writer.writerow([
            group["id"], group["name"],
            sub["id"], sub["name"], sub.get("description", ""),
            sub.get("assigned_team", ""), sub.get("poc_primary", ""),
            len(examples), *examples, *([""] * (max_examples - len(examples))),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=taxonomy.csv"},
    )


_IMPORT_CSV_MAX_BYTES = 20 * 1024 * 1024
_IMPORT_SKIPPED_REPORT_LIMIT = 50


@app.post("/api/tickets/import-csv")
def import_tickets_csv(
    file: UploadFile = File(...),
    dry_run: bool = False,
    user: str = Depends(require_login),
    x_taxonomy_password: str | None = Header(None),
):
    """
    The Taxonomy tab's "Upload tickets CSV": a Zoho "Instructors Ticketing
    System" export, upserted by Ticket ID into the tickets table - new ids
    are created (left pending for the auto-classify loop), known ids get
    their Zoho-side data refreshed. See app/zoho_csv.py's plan_import for
    exactly what is and isn't overwritten.

    Behind the same taxonomy-edit password as PUT /api/taxonomy (X-Taxonomy-
    Password header, checked on every call). dry_run=true returns the same
    counts without writing anything - the UI previews with it first and
    asks for confirmation before the real apply.
    """
    _require_taxonomy_password(x_taxonomy_password)
    content = file.file.read(_IMPORT_CSV_MAX_BYTES + 1)
    if len(content) > _IMPORT_CSV_MAX_BYTES:
        raise HTTPException(413, f"CSV is larger than {_IMPORT_CSV_MAX_BYTES // (1024 * 1024)} MB.")
    try:
        rows = zoho_csv.parse_csv(content)
    except (ValueError, UnicodeDecodeError, csv.Error) as e:
        raise HTTPException(400, f"Couldn't read CSV: {e}")

    if not dry_run and not _import_lock.acquire(blocking=False):
        raise HTTPException(409, "Another ticket import is still running - wait for it to finish.")
    try:
        # One fresh read of the whole table rather than a lookup per row (a
        # network round trip each on Turso). Oldest first, so a duplicated
        # zoho_ticket_id maps to its most recent row - same as
        # db.get_ticket_by_zoho_id.
        db._invalidate_list_all_cache()
        existing_by_zoho_id = {
            t["zoho_ticket_id"]: t for t in db.list_all_tickets() if t.get("zoho_ticket_id")
        }
        plan = zoho_csv.plan_import(rows, existing_by_zoho_id)
        for c in plan.creates:
            c.update(_zoho_category_fields(c.get("zoho_subcategory")))
    except Exception:
        if not dry_run:
            _import_lock.release()
        raise

    summary = {
        "dry_run": dry_run,
        "rows": len(rows),
        "created": len(plan.creates),
        # Of those, how many have no taxonomy match for their Zoho
        # subcategory and so land in human review uncategorised.
        "created_unmatched": sum(1 for c in plan.creates if not c.get("category_id")),
        "updated": len(plan.updates),
        "unchanged": plan.unchanged,
        "newer_in_db": plan.newer_in_db,
        "skipped_count": len(plan.skipped),
        "skipped": plan.skipped[:_IMPORT_SKIPPED_REPORT_LIMIT],
    }
    if dry_run:
        return summary

    # The writes run in a background thread: thousands of rows on Turso take
    # longer than a request should stay open. The UI polls
    # GET /api/tickets/import-csv/status for progress and the final counts.
    _import_job.clear()
    _import_job.update(state="running", done=0, total=len(plan.creates) + len(plan.updates),
                       started_by=user, started_at=time.time(), summary=summary)
    _start_import_job(plan, user)
    return {**summary, "job": dict(_import_job)}


def _start_import_job(plan, user: str) -> None:
    threading.Thread(target=_run_import_job, args=(plan, user), daemon=True).start()


# One import at a time (_import_lock); _import_job is the latest job's state,
# read by the status endpoint. In-process only - a restart mid-import loses
# the job, but every committed chunk stays and re-uploading finishes the rest.
_import_lock = threading.Lock()
_import_job: dict = {"state": "idle"}


def _run_import_job(plan, user: str) -> None:
    def progress(done, total):
        _import_job["done"] = done

    try:
        result = db.bulk_import_tickets(plan.creates, plan.updates, on_progress=progress)
        _import_job.update(state="done", result=result, finished_at=time.time())
        logger.info(
            "tickets CSV import by %s: %d created, %d updated, %d already existed, "
            "%d unchanged, %d newer in db, %d skipped",
            user, result["created"], result["updated"], result["already_existed"],
            plan.unchanged, plan.newer_in_db, len(plan.skipped),
        )
    except Exception as e:
        logger.error("tickets CSV import by %s failed after %s rows: %s", user, _import_job.get("done"), e)
        _import_job.update(state="error", error=str(e)[:500], finished_at=time.time())
    finally:
        _import_lock.release()


@app.get("/api/tickets/import-csv/status")
def import_tickets_csv_status(user: str = Depends(require_login)):
    return dict(_import_job)


def _filter_tickets_by_date_range(tickets: list[dict], date_from: str | None, date_to: str | None) -> list[dict]:
    if date_from:
        start_ts = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        tickets = [t for t in tickets if t["created_at"] >= start_ts]
    if date_to:
        end_ts = (datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp()
        tickets = [t for t in tickets if t["created_at"] < end_ts]
    return tickets


@app.get("/api/routing-log")
def get_routing_log(date_from: str | None = None, date_to: str | None = None, user: str = Depends(require_login)):
    """
    The Log page: one row per ticket (instructor's pick, model's pick and
    confidence, decision, final category, right or wrong) plus the numbers
    for tuning the override threshold - see app/routing_log.py. Uploaded
    history is left out (the model never saw it); drafts already are.
    [date_from, date_to] are UTC calendar days, as /api/tickets/range.
    """
    tickets = [
        t for t in _filter_tickets_by_date_range(db.list_all_tickets(), date_from, date_to)
        if not _is_upload_history(t)
    ]
    rows = routing_log.build_rows(tickets, db.list_corrections())
    return {
        "rows": rows,
        "summary": routing_log.learn(rows, config.REPORTER_OVERRIDE_MIN_CONFIDENCE),
        "drafts": db.count_drafts(),
    }


TICKET_LOG_LIMIT = 300
TICKET_LOG_FIRST_LINE_MAX = 140


def _ticket_log_item(t: dict, flags: dict) -> dict:
    text = t.get("original_text") or ""
    first = text.strip().split("\n", 1)[0].strip()
    if len(first) > TICKET_LOG_FIRST_LINE_MAX:
        first = first[:TICKET_LOG_FIRST_LINE_MAX] + "…"
    f = flags.get(t["id"]) or {}
    return {
        "id": t["id"],
        "zoho_ticket_id": t.get("zoho_ticket_id"),
        "created_at": t.get("created_at"),
        "first_line": first,
        "final": ticket_events.pick_text(t.get("category_id")),
        "has_error": bool(f.get("errors")),
        "has_timeline": bool(f.get("events")),
        "submitted": t.get("status") != "draft",
    }


@app.get("/api/ticket-log")
def get_ticket_log(
    q: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    errors_only: bool = False,
    unsubmitted: bool = False,
    user: str = Depends(require_login),
):
    """The Log page's list: newest first, capped at 300; per-ticket timelines come from /api/ticket-log/{id}."""
    if unsubmitted:
        tickets = db.list_drafts()
    else:
        tickets = [t for t in db.list_all_tickets() if not _is_upload_history(t)]
    tickets = _filter_tickets_by_date_range(tickets, date_from, date_to)
    needle = q.strip().lower()
    if needle:
        tickets = [t for t in tickets if needle in (t.get("zoho_ticket_id") or "").lower()]
    flags = db.event_flags()
    if errors_only:
        tickets = [t for t in tickets if (flags.get(t["id"]) or {}).get("errors")]
    tickets.sort(key=lambda t: t["created_at"], reverse=True)
    return {
        "tickets": [_ticket_log_item(t, flags) for t in tickets[:TICKET_LOG_LIMIT]],
        "total": len(tickets),
    }


@app.get("/api/ticket-log/{ticket_id}")
def get_ticket_log_detail(ticket_id: str, user: str = Depends(require_login)):
    t = db.get_ticket(ticket_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    events = db.list_events(ticket_id)
    item = _ticket_log_item(t, {ticket_id: {
        "events": len(events), "errors": sum(1 for e in events if e["kind"] == "error"),
    }})
    item["issue_text"] = t.get("original_text") or ""
    return {"ticket": item, "events": events}


def _resolve_leaf_for_zoho(category_id: str | None) -> tuple[dict, bool]:
    """
    Resolves category_id to its taxonomy leaf for the webhook's
    category_of_the_issue/sub_category_of_the_issue response fields, which
    are mandatory on the Zoho side - this must never return a leaf that
    would make those come back blank/null.

    Falls back to config.ZOHO_FALLBACK_CATEGORY_ID (the taxonomy's own
    catch-all "Insufficient Information" leaf) whenever category_id is
    missing entirely (never classified yet, or classify() itself failed)
    or no longer exists in the current taxonomy (orphaned by a later
    Taxonomy tab edit - see taxonomy.py's reload() note). As an absolute
    last resort - the fallback id itself somehow missing too - falls back
    again to the first leaf the current taxonomy has at all, so this simply
    cannot return a leaf of None.

    Second return value is True whenever a fallback was used, so callers
    can flag the ticket (fallback_reason) instead of treating this as a
    real classification.
    """
    leaf = taxonomy.get(category_id) if category_id else None
    if leaf is not None:
        return leaf, False

    fallback = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    if fallback is not None:
        return fallback, True

    for leaf_id in taxonomy.category_ids:
        any_leaf = taxonomy.get(leaf_id)
        if any_leaf:
            return any_leaf, True
    return {"name": "Unclassified", "parent_name": "Unclassified"}, True


@app.post("/api/webhooks/zoho/tickets")
def webhook_new_zoho_ticket(payload: dict, _: None = Depends(require_webhook_secret)):
    """
    Zoho Creator's "On Add/Edit" workflow calls this directly with the record's
    fields (see zoho-invoke-url-setup.md for the Deluge script) - a new
    ticket gets classified the moment it's created in Zoho, and every
    subsequent edit (status change, POC acknowledgment, worklog, etc.)
    refreshes the stored data - no polling.

    zoho_ticket_id is OPTIONAL, which puts this endpoint in one of two
    modes:

    - Present -> persist mode (the original behavior): upserts by
      zoho_ticket_id, classifying + storing a real ticket the first time an
      id is seen, and refreshing raw_payload/zoho_category/zoho_subcategory/
      original_text in place on every call after that.
    - Absent -> suggestion mode: for a "Suggest category" action in Zoho
      that runs BEFORE a record is submitted/saved - i.e. before Zoho has
      generated a Ticket_ID for it. Classifies whatever draft
      issue_in_detail text is passed in and returns a preview, and stores
      it as a 'draft' ticket (no Zoho id) carrying the instructor's pick,
      the model's pick and the decision for the routing log. The On Add call
      that follows claims the oldest matching draft from the last 2 hours
      instead of creating a new row (see _DRAFT_MATCH_WINDOW_SECONDS). Drafts
      never claimed - abandoned forms, or text edited after the suggestion -
      stay drafts: never deleted, hidden from every listing and count.

    Auth is a shared secret (X-Webhook-Secret, see require_webhook_secret)
    rather than the session login the UI uses, since Deluge can't hold a
    browser session. Classification runs with the server-side
    OPENROUTER_API_KEY (no UI operator is present to supply one per-request).

    Accepts a plain dict rather than a typed model on purpose: the real
    "On Add/Edit" workflow sends dozens of fields (priority, assigned team,
    POC/worklog history, session/evaluation ids, etc.), and the whole
    payload is kept as-is (see raw_payload below) for later analytics
    without this endpoint needing a code change every time Zoho's form
    gains a field. Only zoho_ticket_id/issue_in_detail/category_of_the_issue/
    sub_category_of_the_issue are pulled out specifically, for classification
    and the Zoho-tag comparison - everything else (ticket_status,
    acknowledgement_from_the_poc, worklog_from_the_poc, etc.) just rides
    along in raw_payload and shows up in the portal's "Ticket Details" table.

    Returns the model's own classification alongside the ack, in every
    branch: {"ok": true, "category_of_the_issue": "<parent group NAME, e.g.
    "QA Report / Instructor Evaluation">", "sub_category_of_the_issue":
    "<leaf NAME, e.g. "Feedback Too Generic or Vague">", "needs_review": false}
    - human-readable names, not taxonomy ids. Both name fields come from the
    same _resolve_leaf_for_zoho(category_id) lookup - category_of_the_issue
    is its leaf's ["parent_name"], sub_category_of_the_issue is its ["name"].
    In the persist-mode update branch (an already-known zoho_ticket_id) these
    reflect whatever category is already stored for that ticket rather than
    a fresh prediction - see the note below on why edits never reclassify.

    category_of_the_issue/sub_category_of_the_issue are mandatory fields on
    the Zoho side, so this endpoint is guaranteed to NEVER send them back
    null/blank: _resolve_leaf_for_zoho falls back to
    config.ZOHO_FALLBACK_CATEGORY_ID (the taxonomy's catch-all
    "Insufficient Information" leaf) whenever the real category_id can't be
    resolved - never classified yet, classify() itself raised (rate limit
    exhausted with no Cloudflare fallback, network error, malformed model
    output, etc. - caught here rather than propagating to a 500), or the
    stored id was orphaned by a later Taxonomy tab edit. needs_review=true
    marks exactly those cases so Zoho-side automation (or a human) can tell
    a forced fallback apart from a genuine classification; the affected
    ticket also gets fallback_reason set (see db.py's tickets.fallback_reason)
    so it can be found and reclassified.

    Upserts by zoho_ticket_id: the first time a ticket id is seen, it's
    created and classified as before. Every call after that (an edit, or a
    retried "On Add" after a slow/cold-start response) updates that same
    row's raw_payload/zoho_category/zoho_subcategory/original_text in place
    instead of creating a second row, and otherwise never touches
    category_id/confidence/reasoning/status - so an unrelated status change
    in Zoho can never silently undo a human's correction in this portal -
    with one deliberate exception: if sub_category_of_the_issue actually
    changed to a different value that resolves to a different taxonomy leaf
    than what we have stored, that's a real ticket transfer (someone in
    Zoho moved it to the right team after we routed it wrong), and gets
    treated as an implicit correction - category_id/status/confidence do
    update, and the classifier learns from it via memory.add_example(), the
    same as a manual "Correct" click in this portal. See
    _detect_zoho_transfer for the exact conditions.
    """
    issue_text = str(payload.get("issue_in_detail") or "").strip()
    if not issue_text:
        raise HTTPException(400, "issue_in_detail is required")

    zoho_ticket_id = str(payload.get("zoho_ticket_id") or "").strip()

    if not zoho_ticket_id:
        # Suggestion mode - see docstring above. No DB writes of any kind.
        # Whatever the instructor picked on the form before this pre-submit
        # call - the whole point of the reporter hint, since this response
        # overwrites those same fields (see classifier.ReporterHint).
        hint = _reporter_hint_from(
            payload.get("category_of_the_issue"), payload.get("sub_category_of_the_issue")
        )
        started = time.time()
        instructor_pick = (
            str(payload.get("sub_category_of_the_issue") or "").strip()
            or str(payload.get("category_of_the_issue") or "").strip() or None
        )
        instructor_leaf = hint.leaf_id if hint else None
        threshold = config.REPORTER_OVERRIDE_MIN_CONFIDENCE
        failure_text = None
        try:
            _require_webhook_classify_key()
            result = classifier.classify(issue_text, config.OPENROUTER_API_KEY, reporter_hint=hint)
            category_id = result.category_id
            draft_id = _store_presubmit_draft(issue_text, payload, hint, result)
        except Exception as e:
            logger.error(f"Zoho webhook suggestion-mode classify() failed: {e}")
            draft_id = _store_presubmit_draft(issue_text, payload, hint, None, failure=str(e))
            result = None
            failure_text = str(e)[:1000]
            echo = _reporter_pick_echo(payload)
            if echo:
                _event(draft_id, "presubmit", lambda: ticket_events.presubmit_line(
                    None, instructor_leaf, instructor_pick, instructor_leaf or "", False, threshold),
                    started=started)
                _event(draft_id, "error", "Classification failed", error=failure_text)
                return {"ok": True, **echo, "needs_review": True}
            category_id = None
        leaf, used_fallback = _resolve_leaf_for_zoho(category_id)
        _event(draft_id, "presubmit", lambda: ticket_events.presubmit_line(
            result, instructor_leaf, instructor_pick, leaf["id"], used_fallback, threshold),
            started=started)
        if failure_text is not None:
            _event(draft_id, "error", "Classification failed", error=failure_text)
        return {
            "ok": True,
            "category_of_the_issue": leaf["parent_name"],
            "sub_category_of_the_issue": leaf["name"],
            "needs_review": used_fallback,
        }

    zoho_category = str(payload.get("category_of_the_issue") or "").strip() or None
    zoho_subcategory = str(payload.get("sub_category_of_the_issue") or "").strip() or None

    existing = db.get_ticket_by_zoho_id(zoho_ticket_id)
    if existing:
        auto_correction = _detect_zoho_transfer(existing, zoho_subcategory)
        old_status = (existing.get("raw_payload") or {}).get("ticket_status")
        new_status = payload.get("ticket_status")
        if old_status != new_status:
            _event(existing["id"], "edit", lambda: ticket_events.edit_line(old_status, new_status))

        db.update_ticket(
            existing["id"],
            original_text=issue_text,
            full_context=issue_text,
            zoho_category=zoho_category,
            zoho_subcategory=zoho_subcategory,
            raw_payload=payload,
        )

        if auto_correction:
            # A human moved this ticket to a different category in Zoho -
            # log and learn from it exactly like the in-portal "Correct"
            # button does (see correct_ticket below), just detected from
            # the edit itself instead of a manual click.
            db.log_correction(existing["id"], existing.get("category_id"), auto_correction, corrected_by="zoho-transfer")
            _event(existing["id"], "transfer", lambda: ticket_events.transfer_line(
                existing.get("category_id"), auto_correction))
            try:
                memory.add_example(
                    issue_text, auto_correction, config.OPENAI_API_KEY,
                    source="correction", ticket_id=existing["id"],
                )
            except Exception as e:
                logger.error(f"Zoho-transfer memory.add_example failed for ticket {existing['id']}: {e}")
            db.update_ticket(
                existing["id"],
                status="corrected",
                category_id=auto_correction,
                confidence=1.0,
                reasoning="Category changed in Zoho after our classification - treated as an implicit human correction.",
                fallback_reason=None,
            )
            existing = db.get_ticket(existing["id"])

        leaf, used_fallback = _resolve_leaf_for_zoho(existing.get("category_id"))
        if used_fallback:
            # Either never classified yet, or category_id is set but no
            # longer resolves (orphaned by a later taxonomy edit) - flag it
            # so it surfaces for reclassification instead of silently
            # sending Zoho the fallback category on every future edit too.
            reason = (
                f"orphaned_category_id:{existing['category_id']}"
                if existing.get("category_id") else "never_classified"
            )
            db.update_ticket(existing["id"], fallback_reason=reason)
            _event(existing["id"], "error", "Sent the fallback category", error=reason)
        return {
            "ok": True,
            "updated": True,
            "ticket_id": existing["id"],
            "category_of_the_issue": leaf["parent_name"],
            "sub_category_of_the_issue": leaf["name"],
            "needs_review": used_fallback,
        }

    draft = db.claim_draft(
        issue_text, time.time() - _DRAFT_MATCH_WINDOW_SECONDS,
        zoho_ticket_id=zoho_ticket_id,
        zoho_category=zoho_category,
        zoho_subcategory=zoho_subcategory,
        raw_payload=payload,
        status="pending",
    )
    if draft:
        ticket_id = draft["id"]
    else:
        ticket_id = db.create_ticket(
            issue_text,
            zoho_ticket_id=zoho_ticket_id,
            zoho_category=zoho_category,
            zoho_subcategory=zoho_subcategory,
            raw_payload=payload,
        )
    first_zoho_id = _mark_if_duplicate(ticket_id, issue_text, payload)
    if first_zoho_id is not None:
        _event(ticket_id, "duplicate", lambda: ticket_events.duplicate_line(first_zoho_id or None))

    adopted = _adopt_presubmit_classification(ticket_id, draft, zoho_subcategory)
    if adopted:
        _event(ticket_id, "on_add", lambda: ticket_events.on_add_line(True, adopted))
        leaf = taxonomy.get(adopted)
        return {
            "ok": True,
            "category_of_the_issue": leaf["parent_name"],
            "sub_category_of_the_issue": leaf["name"],
            "needs_review": False,
        }

    # The instructor's real pick: from the draft when there is one - by now
    # the form's own category fields hold the pre-submit suggestion instead.
    if draft:
        hint = _reporter_hint_from(draft.get("reporter_category"), draft.get("reporter_subcategory"))
    else:
        hint = _reporter_hint_from(zoho_category, zoho_subcategory)
        db.update_ticket(ticket_id, **_reporter_fields(zoho_category, zoho_subcategory, hint))
    classify_failed_reason = None
    started = time.time()
    try:
        _require_webhook_classify_key()
        result = _run_classification_and_persist(
            ticket_id, issue_text, clarification_turns=0, api_key=config.OPENROUTER_API_KEY,
            reporter_hint=hint,
        )
        category_id = result.category_id
        _event(ticket_id, "on_add", lambda: ticket_events.on_add_line(False, category_id), started=started)
    except Exception as e:
        logger.error(f"Zoho webhook classify() failed for new ticket {ticket_id}: {e}")
        _event(ticket_id, "on_add", lambda: ticket_events.on_add_line(False, hint.leaf_id if hint else None))
        _event(ticket_id, "error", "Classification failed", error=str(e)[:1000], started=started)
        classify_failed_reason = f"classify_error:{str(e)[:300]}"
        # Still route by the instructor's own subcategory when it resolves,
        # so the ticket reaches that POC's queue while it waits for review.
        failsafe_leaf = hint.leaf_id if hint else None
        db.update_ticket(
            ticket_id,
            status="needs_human_review",
            category_id=failsafe_leaf,
            reasoning=(
                f"Automatic classification failed: {e}"
                + (" - kept the instructor's own pick." if failsafe_leaf else "")
            ),
            fallback_reason=classify_failed_reason,
        )
        echo = _reporter_pick_echo(payload)
        if echo:
            return {"ok": True, **echo, "needs_review": True}
        category_id = None

    leaf, used_fallback = _resolve_leaf_for_zoho(category_id)
    if used_fallback and classify_failed_reason is None and category_id:
        # classify() itself succeeded but the id it returned doesn't resolve
        # - an extremely narrow race against a concurrent taxonomy edit (see
        # _resolve_leaf_for_zoho's docstring).
        db.update_ticket(ticket_id, fallback_reason=f"orphaned_category_id:{category_id}")

    return {
        "ok": True,
        "category_of_the_issue": leaf["parent_name"],
        "sub_category_of_the_issue": leaf["name"],
        "needs_review": used_fallback,
    }


# How old a pre-submit draft can be and still be claimed by an On Add call
# with the same text - long enough for an instructor to finish the form,
# short enough that an old abandoned draft never attaches to a new ticket.
_DRAFT_MATCH_WINDOW_SECONDS = 2 * 3600
# Same text from the same instructor within this long = a duplicate ticket.
_DUPLICATE_WINDOW_SECONDS = 7 * 24 * 3600


def _reporter_fields(category, subcategory, hint: classifier.ReporterHint | None) -> dict:
    """The instructor's own pick, for the routing log."""
    return {
        "reporter_category": str(category or "").strip() or None,
        "reporter_subcategory": str(subcategory or "").strip() or None,
        "reporter_leaf_id": hint.leaf_id if hint else None,
    }


def _decision_fields(result: ClassificationResult) -> dict:
    """The model's own pick and the agreed/kept/overrode decision, for the
    routing log. With no subcategory picked by the instructor there's
    nothing to weigh, so the model's pick is the result itself and the
    decision is 'none' (NULL is left for tickets from before the log)."""
    if result.decision is None:
        return {"model_category_id": result.category_id, "model_confidence": result.confidence, "decision": "none"}
    return {
        "model_category_id": result.model_category_id,
        "model_confidence": result.model_confidence,
        "decision": result.decision,
    }


def _event(ticket_id: str | None, kind: str, summary, error: str | None = None,
           started: float | None = None) -> None:
    """Add one line to a ticket's timeline. `summary` is a string or a
    zero-argument callable that builds it, so a failing line builder is
    caught here too. Never raises: Zoho must always get its category back."""
    if ticket_id is None:
        return
    try:
        text = summary() if callable(summary) else summary
        duration_ms = int((time.time() - started) * 1000) if started is not None else None
        db.add_event(ticket_id, kind, text, error=error, duration_ms=duration_ms)
    except Exception as e:
        logger.error(f"recording a {kind} event for ticket {ticket_id} failed: {e}")


def _store_presubmit_draft(issue_text: str, payload: dict, hint, result: ClassificationResult | None,
                           failure: str | None = None) -> str | None:
    """
    Pre-submit call -> a 'draft' ticket for the On Add call to claim (see
    _adopt_presubmit_classification). A draft with no category_id records
    that the pre-submit classification failed. Never raises: the
    instructor's form must get its suggestion even if the database is down.
    """
    fields = _reporter_fields(payload.get("category_of_the_issue"), payload.get("sub_category_of_the_issue"), hint)
    if result is not None:
        fields.update(category_id=result.category_id, confidence=result.confidence,
                      reasoning=result.reasoning, **_decision_fields(result))
    else:
        fields.update(reasoning=f"Pre-submit classification failed: {failure}"[:500])
    try:
        return db.create_draft(issue_text, payload, **fields)
    except Exception as e:
        logger.error(f"storing the pre-submit draft failed: {e}")
        return None


def _instructor_key(payload: dict | None) -> str | None:
    payload = payload or {}
    who = str(payload.get("ticket_raised_by") or payload.get("instructor_id") or "").strip().lower()
    return who or None


def _same_text_key(text: str | None) -> str:
    return " ".join(str(text or "").lower().split())


def _mark_if_duplicate(ticket_id: str, issue_text: str, payload: dict) -> str | None:
    """
    Same text from the same instructor (ticket_raised_by) within the last
    7 days -> duplicate_of points at the first such ticket, so the routing
    log counts the issue once. Returns that first ticket's Zoho id ('' when
    it has none), or None when this isn't a duplicate. Never raises - On Add
    must still store the ticket.
    """
    who = _instructor_key(payload)
    if not who:
        return None
    try:
        same = _same_text_key(issue_text)
        for t in db.list_tickets_since(time.time() - _DUPLICATE_WINDOW_SECONDS):
            if t["id"] != ticket_id and _instructor_key(t.get("raw_payload")) == who \
                    and _same_text_key(t.get("original_text")) == same:
                first_id = t.get("duplicate_of") or t["id"]
                db.update_ticket(ticket_id, duplicate_of=first_id)
                first = t if first_id == t["id"] else db.get_ticket(first_id)
                return (first or {}).get("zoho_ticket_id") or ""
    except Exception as e:
        logger.error(f"duplicate check failed for ticket {ticket_id}: {e}")
    return None


def _adopt_presubmit_classification(ticket_id: str, draft: dict | None, zoho_subcategory: str | None) -> str | None:
    """
    The pre-submit Deluge script already classified this ticket (stored as
    the draft this On Add call just claimed) and wrote the result into the
    form's category fields before it was saved, so the On Add call must not
    classify it a second time. Stores that classification on the ticket and
    returns its leaf id, or returns None (caller classifies as before) when
    there's nothing trustworthy to adopt:

    - the pre-submit classification failed (the draft has no category; the
      form's fields then hold the instructor's own pick or the fallback
      leaf, not a model answer) - so this is the first real
      classification, not a repeat;
    - the Zoho subcategory doesn't resolve to a taxonomy leaf;
    - no draft AND the subcategory is the catch-all fallback leaf (most
      likely a forced fallback, not a model answer).

    A draft for the same leaf keeps its confidence and reasoning. A
    different leaf means someone changed the field on the form after our
    suggestion - the form wins.
    """
    found = draft is not None
    if found and not draft.get("category_id"):
        return None
    leaf_id = _resolve_taxonomy_leaf_by_name(zoho_subcategory)
    if leaf_id is None:
        return None
    if not found and leaf_id == config.ZOHO_FALLBACK_CATEGORY_ID:
        return None

    if found and draft["category_id"] == leaf_id:
        confidence, reasoning = draft.get("confidence"), draft.get("reasoning")
    elif found:
        confidence = None
        reasoning = (
            f"Category changed on the Zoho form after our suggestion ({draft['category_id']}) - "
            "kept the form's value."
        )
    else:
        confidence = None
        reasoning = "Category from the Zoho form's pre-submit classification - not re-classified."
    status = (
        "needs_human_review"
        if confidence is not None and confidence < config.CONFIDENCE_THRESHOLD
        else "classified"
    )
    db.update_ticket(ticket_id, status=status, category_id=leaf_id, confidence=confidence, reasoning=reasoning)
    return leaf_id


def _normalize_label(s: str) -> str:
    """Lowercase and collapse separators so '/','-','_' and extra spaces don't
    cause a spurious mismatch between our taxonomy names and Zoho's own text."""
    for ch in ("/", "-", "_"):
        s = s.replace(ch, " ")
    return " ".join(s.lower().split())


def _labels_loosely_match(a: str | None, b: str | None) -> bool | None:
    """
    None when there's nothing to compare (Zoho sent no tag); otherwise
    whether the two labels are the same or one contains the other, e.g.
    'Rubric Discrepancy' matching 'Scorecard / Rubric Discrepancy'. Exact
    equality would miss most real matches since our taxonomy names and
    Zoho's free-text category fields aren't guaranteed to use the same
    wording - this is a rough accuracy signal for human review, not a
    substitute for someone actually checking each ticket.
    """
    if not a or not b:
        return None
    na, nb = _normalize_label(a), _normalize_label(b)
    return na == nb or na in nb or nb in na


def _resolve_taxonomy_leaf_by_name(name: str | None) -> str | None:
    """
    Best-effort reverse lookup: a Zoho category/subcategory free-text value
    -> our taxonomy leaf id. Exact normalized match preferred; falls back to
    the same loose substring match _labels_loosely_match uses for the (much
    weaker) zoho_agrees display signal, but only when it's unambiguous.

    Returns None rather than guessing when nothing matches or more than one
    leaf would - this feeds directly into the classifier's training memory
    (see _detect_zoho_transfer below), so a wrong guess here would quietly
    poison future classifications, not just mislabel one dashboard cell.
    """
    if not name:
        return None
    normalized_target = _normalize_label(name)
    exact = [
        leaf_id for leaf_id in taxonomy.category_ids
        if _normalize_label(taxonomy.get(leaf_id)["name"]) == normalized_target
    ]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None  # duplicate names in taxonomy.json - ambiguous, don't guess
    loose = [
        leaf_id for leaf_id in taxonomy.category_ids
        if _labels_loosely_match(taxonomy.get(leaf_id)["name"], name)
    ]
    return loose[0] if len(loose) == 1 else None


def _reporter_hint_from(category_name, subcategory_name) -> classifier.ReporterHint | None:
    """
    Zoho form's Category_Of_The_Issue / optional Sub_Category_Of_The_Issue
    text -> a ReporterHint for classify(), or None when there's nothing
    usable. The subcategory wins when it resolves (it's the more specific
    pick, and its parent is the group); otherwise an exact category-group
    name match gives a group-only hint. A pick of the catch-all fallback
    group ("Other / Unclear") carries no intent, so it's dropped rather than
    anchoring the model to "no routing required".
    """
    subcategory_name = str(subcategory_name or "").strip() or None
    category_name = str(category_name or "").strip() or None

    leaf_id = _resolve_taxonomy_leaf_by_name(subcategory_name)
    if leaf_id:
        group_id = taxonomy.get(leaf_id)["parent_id"]
    else:
        target = _normalize_label(category_name) if category_name else None
        matches = [g["id"] for g in taxonomy.groups if target and _normalize_label(g["name"]) == target]
        if len(matches) != 1:
            return None
        group_id = matches[0]

    fallback = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    if fallback and group_id == fallback["parent_id"]:
        return None
    return classifier.ReporterHint(group_id=group_id, leaf_id=leaf_id)


def _require_webhook_classify_key() -> None:
    """
    Raised inside the webhook's classify try-blocks rather than as an HTTP
    500 up front: a missing OPENROUTER_API_KEY is just one more way
    classification can fail, and gets the same failsafe as an exhausted
    key - the instructor's own pick echoed back (or the fallback leaf),
    needs_review=true, and the new ticket still stored. A 500 here made
    the pre-submit script block the ticket ("Unable to classify") and the
    On Add call drop it from the portal entirely.
    """
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set on the server")


def _reporter_pick_echo(payload: dict) -> dict | None:
    """
    Failsafe for when classify() itself fails (OpenRouter key/credits
    exhausted with no Cloudflare fallback, embeddings key exhausted, network
    error, ...): hand Zoho back exactly the category/subcategory the
    instructor already picked, verbatim, instead of overwriting them with
    the catch-all "Insufficient Information" leaf. Those are values Zoho's
    own dropdowns produced, so they're valid on the Zoho side even if they
    don't resolve to our taxonomy. Both are mandatory in Zoho, so this only
    applies when both are present - a category-only pick still falls back
    to _resolve_leaf_for_zoho as before.
    """
    category = str(payload.get("category_of_the_issue") or "").strip()
    subcategory = str(payload.get("sub_category_of_the_issue") or "").strip()
    if not category or not subcategory:
        return None
    return {"category_of_the_issue": category, "sub_category_of_the_issue": subcategory}


def _zoho_category_fields(zoho_subcategory: str | None) -> dict:
    """
    Ticket fields for a ticket whose category comes from Zoho itself rather
    than from our model - CSV-uploaded history, already categorised in
    Zoho, so classifying it again would only spend credits. A subcategory
    that resolves to a taxonomy leaf is stored as-is (status 'classified',
    no model confidence); one that doesn't goes to human review uncategorised
    instead of to the model.
    """
    leaf_id = _resolve_taxonomy_leaf_by_name(zoho_subcategory)
    if leaf_id:
        return {
            "status": "classified",
            "category_id": leaf_id,
            "confidence": None,
            "reasoning": "Category taken from Zoho (uploaded data) - not re-classified.",
        }
    return {
        "status": "needs_human_review",
        "category_id": None,
        "confidence": None,
        "reasoning": (
            f"Zoho subcategory {zoho_subcategory!r} doesn't match the taxonomy - not sent to the "
            "classifier (uploaded data). Use \"Correct it\" to set one."
            if zoho_subcategory else
            "No Zoho subcategory - not sent to the classifier (uploaded data). "
            "Use \"Correct it\" to set one."
        ),
    }


def _is_csv_imported(ticket: dict) -> bool:
    # added_time only ever comes from a CSV export (app/zoho_csv.py's
    # FIELD_MAP) - the live webhook payload never carries it.
    return "added_time" in (ticket.get("raw_payload") or {})


_UPLOAD_REASONING_MARK = "(uploaded data)"  # in every reasoning _zoho_category_fields writes


def _is_upload_history(ticket: dict) -> bool:
    """
    A ticket created by a CSV upload whose Zoho data still IS that upload -
    history that was already worked and closed in Zoho before it reached
    this app. Both halves matter: a live webhook ticket that an upload
    merely refreshed carries CSV data but was never created by one (its
    reasoning is a real classification), and an uploaded ticket that later
    gets a live Zoho edit has its raw_payload replaced by the webhook (no
    CSV-only added_time any more), so from then on it's tracked live.
    """
    return _UPLOAD_REASONING_MARK in (ticket.get("reasoning") or "") and _is_csv_imported(ticket)


def _settle_pending_csv_imports() -> int:
    """
    Gives every still-pending CSV-imported ticket its Zoho category instead
    of leaving it for the classifier (see _zoho_category_fields) - covers
    uploads made before uploads stopped being queued for classification.
    Runs before each classify batch and once at startup. Returns how many
    tickets it settled.
    """
    updates = [
        (t["id"], _zoho_category_fields(t.get("zoho_subcategory")))
        for t in db.list_pending_tickets() if _is_csv_imported(t)
    ]
    if updates:
        db.bulk_import_tickets([], updates)
        logger.info("settled %d pending CSV-imported ticket(s) from their Zoho category", len(updates))
    return len(updates)


def _reporter_hint_for_ticket(ticket: dict) -> classifier.ReporterHint | None:
    return _reporter_hint_from(ticket.get("zoho_category"), ticket.get("zoho_subcategory"))


def _detect_zoho_transfer(existing: dict, new_subcategory: str | None) -> str | None:
    """
    A ticket "transfer" in Zoho - a human changing sub_category_of_the_issue
    on a ticket we already classified, because our routing (or whoever
    originally raised the ticket) sent it to the wrong team - is the
    strongest correction signal this app ever sees, but until now it never
    reached app/memory.py: the webhook only ever refreshed zoho_subcategory
    for display/comparison (see zoho_agrees), and only the in-portal
    "Correct" button (see correct_ticket below) fed the classifier's
    learning loop. This detects that same real-world event straight from
    the Zoho edit itself, so a transfer teaches the classifier exactly like
    a manual correction does - no one has to also click "Correct" in this
    portal for the lesson to land.

    Returns the resolved taxonomy leaf id to auto-correct to, or None when
    nothing qualifies as a real transfer:
    - the ticket must already have had a real prior zoho_subcategory - its
      first-ever category isn't a "transfer" from anything.
    - the old and new subcategory text must actually differ (a same-value
      edit, or an unrelated field update on the ticket, isn't a transfer).
    - the new subcategory name must resolve unambiguously to one of our
      taxonomy leaves (see _resolve_taxonomy_leaf_by_name) - free text that
      doesn't match anything is left alone rather than guessed at.
    - that resolved leaf must differ from what we're currently storing as
      this ticket's category_id - otherwise this is just Zoho's own field
      catching up to a classification we already made (our webhook response
      gets written back into the record by Zoho's own workflow), not a
      human overriding it.
    """
    prior_subcategory = existing.get("zoho_subcategory")
    if not prior_subcategory or not new_subcategory:
        return None
    if _normalize_label(prior_subcategory) == _normalize_label(new_subcategory):
        return None

    resolved = _resolve_taxonomy_leaf_by_name(new_subcategory)
    if resolved is None or resolved == existing.get("category_id"):
        return None
    return resolved


def _run_classification_and_persist(
    ticket_id: str,
    context_text: str,
    clarification_turns: int,
    api_key: str,
    reporter_hint: classifier.ReporterHint | None = None,
):
    result = classifier.classify(context_text, api_key, reporter_hint=reporter_hint)

    if classifier.should_finalize(result, clarification_turns):
        if result.confidence < config.CONFIDENCE_THRESHOLD and clarification_turns >= config.MAX_CLARIFICATION_TURNS:
            # Ran out of clarification budget and still not confident -> human review, don't guess.
            db.update_ticket(
                ticket_id,
                status="needs_human_review",
                category_id=result.category_id,
                confidence=result.confidence,
                reasoning=result.reasoning,
                full_context=context_text,
                **_decision_fields(result),
            )
        else:
            db.update_ticket(
                ticket_id,
                status="classified",
                category_id=result.category_id,
                confidence=result.confidence,
                reasoning=result.reasoning,
                full_context=context_text,
                **_decision_fields(result),
            )
    else:
        db.update_ticket(
            ticket_id,
            status="awaiting_clarification",
            clarification_turns=clarification_turns + 1,
            full_context=context_text,
            category_id=result.category_id,
            confidence=result.confidence,
            reasoning=result.reasoning,
            **_decision_fields(result),
        )
        db.append_turn(ticket_id, "system_question", result.clarifying_question or "Could you provide more detail?")

    return result


def _classify_pending_batch(limit: int, api_key: str) -> dict:
    """
    Classifies up to `limit` tickets left in status='pending' (e.g. from a
    --no-classify historical import) - one-shot, no clarifying-question
    round trip (there's no live user to answer for a batch of old tickets),
    so an ambiguous result goes straight to needs_human_review instead of
    awaiting_clarification, same rule scripts/import_zoho_csv.py uses.

    Stops the batch immediately on a RateLimitError (further calls would
    just fail the same way) but keeps going past other per-ticket errors.
    Used by the background auto-classify loop below.

    Queries only status='pending' tickets (db.list_pending_tickets()) rather
    than reading the whole ticket history - this used to matter for
    Firestore's per-document read quota specifically, and stays cheaper on
    Turso too since it's a server-side WHERE filter, not a Python one.
    Derives "remaining" from that same snapshot plus how many were just
    processed, rather than re-querying to recount - "remaining" is an
    estimate against that snapshot (a ticket that arrived mid-batch won't
    be reflected until the next cycle).
    """
    _settle_pending_csv_imports()
    pending_all = db.list_pending_tickets()
    pending = pending_all[:limit]
    classified_count = 0
    stopped_early = False
    error = None

    for t in pending:
        context_text = (t.get("full_context") or t.get("original_text") or "").strip()
        if not context_text:
            continue
        hint = _reporter_hint_for_ticket(t)
        started = time.time()
        try:
            result = classifier.classify(context_text, api_key, reporter_hint=hint)
        except RateLimitError as e:
            stopped_early = True
            error = str(e)
            _event(t["id"], "error", "Auto-classification failed", error=str(e)[:1000], started=started)
            break
        except Exception as e:
            error = str(e)
            _event(t["id"], "error", "Auto-classification failed", error=str(e)[:1000], started=started)
            continue

        status = "needs_human_review" if (result.needs_clarification or result.confidence < config.CONFIDENCE_THRESHOLD) else "classified"
        db.update_ticket(
            t["id"],
            status=status,
            category_id=result.category_id,
            confidence=result.confidence,
            reasoning=result.reasoning,
            **_decision_fields(result),
            **({} if t.get("reporter_category") or t.get("reporter_subcategory")
               else _reporter_fields(t.get("zoho_category"), t.get("zoho_subcategory"), hint)),
        )
        classified_count += 1
        _event(t["id"], "auto_classify", lambda: ticket_events.auto_classify_line(
            result, hint.leaf_id if hint else None, config.REPORTER_OVERRIDE_MIN_CONFIDENCE), started=started)

    remaining = len(pending_all) - classified_count
    return {"classified": classified_count, "remaining": remaining, "stopped_early": stopped_early, "error": error}


_AUTO_CLASSIFY_INTERVAL_SECONDS = 1800  # 30 min
_AUTO_CLASSIFY_BATCH_SIZE = 10


async def _auto_classify_loop():
    """
    Background retry for pending tickets - picks up automatically once
    OPENROUTER_API_KEY's rate limit (or whatever else caused a stall) clears.
    Small batch size and a long interval so a persistent outage just quietly
    no-ops each cycle instead of hammering a dead API.
    """
    while True:
        await asyncio.sleep(_AUTO_CLASSIFY_INTERVAL_SECONDS)
        if not config.OPENROUTER_API_KEY:
            continue
        try:
            result = _classify_pending_batch(_AUTO_CLASSIFY_BATCH_SIZE, config.OPENROUTER_API_KEY)
            if result["classified"]:
                logger.info(
                    "auto-classify: classified %d pending ticket(s), %d remaining",
                    result["classified"], result["remaining"],
                )
        except Exception as e:
            logger.error("auto-classify loop error: %s", e)
