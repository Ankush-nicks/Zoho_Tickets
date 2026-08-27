import csv
import io
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import logging

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, memory, classifier
from .taxonomy import taxonomy
from .auth import require_login
from .models import (
    LoginRequest,
    NewTicketRequest,
    ClarificationResponse,
    CorrectionRequest,
    TicketStateResponse,
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


def require_api_key(x_openai_api_key: str | None = Header(default=None, alias="X-OpenAI-Api-Key")) -> str:
    """
    The OpenAI key comes from the UI (entered once, kept in the browser's
    localStorage, sent on every request) rather than from a server-side
    .env - falls back to OPENAI_API_KEY if that's set for local/dev use.
    """
    key = x_openai_api_key or config.OPENAI_API_KEY
    if not key:
        raise HTTPException(401, "OpenAI API key required - enter it in the box at the top of the page.")
    return key


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
def startup():
    db.init_db()
    # Vector memory needs an OpenAI key to embed the seed examples, which we
    # don't have until a request carries one - seeding happens lazily on the
    # first classify() call instead (see classifier.classify).


@app.get("/")
def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login")
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/login")
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/")
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
    return {"categories": taxonomy.groups}


@app.get("/api/taxonomy/export.csv")
def export_taxonomy_csv(user: str = Depends(require_login)):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "category_id", "category_name",
        "subcategory_id", "subcategory_name", "description",
        "assigned_team", "poc_primary", "poc_cc",
        "ticket_volume", "data_coverage", "example_count",
    ])
    for group in taxonomy.groups:
        for sub in group.get("subcategories", []):
            writer.writerow([
                group["id"], group["name"],
                sub["id"], sub["name"], sub.get("description", ""),
                sub.get("assigned_team", ""), sub.get("poc_primary", ""), sub.get("poc_cc", ""),
                sub.get("ticket_volume", ""), sub.get("data_coverage", ""),
                len(sub.get("examples", [])),
            ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=taxonomy.csv"},
    )


@app.get("/api/stats")
def get_stats(user: str = Depends(require_login)):
    return db.stats()


# --- Zoho Creator integration ----------------------------------------------

@app.get("/api/zoho/status")
def get_zoho_status(user: str = Depends(require_login)):
    """
    Whether the pull-direction (ZOHO_INVOKE_URL) is still an unset placeholder
    rather than a real Zoho Custom API - never returns the actual URL or key
    value, just enough to render a status indicator in the UI. Irrelevant to
    the push webhook (/api/webhooks/zoho/tickets), which doesn't use these.
    """
    return {
        "invoke_url_is_sample": "REPLACE_WITH_REAL_ZOHO_CUSTOM_API" in config.ZOHO_INVOKE_URL,
        "api_key_is_sample": config.ZOHO_API_KEY == "sample-zoho-key",
    }


@app.post("/api/webhooks/zoho/tickets")
def webhook_new_zoho_ticket(payload: dict, _: None = Depends(require_webhook_secret)):
    """
    PUSH counterpart to the pull-based /api/zoho endpoints above: Zoho
    Creator's "On Add/Edit" workflow calls this directly with the record's
    fields (see zoho-invoke-url-setup.md for the Deluge script) - a new
    ticket gets classified the moment it's created in Zoho, and every
    subsequent edit (status change, POC acknowledgment, worklog, etc.)
    refreshes the stored data - no polling, no round trip back through
    ZOHO_INVOKE_URL.

    Auth is a shared secret (X-Webhook-Secret, see require_webhook_secret)
    rather than the session login the UI uses, since Deluge can't hold a
    browser session. Classification runs with the server-side
    OPENAI_API_KEY (no UI operator is present to supply one per-request).

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

    Intentionally returns only a bare ack, not the classification result
    (category/team/confidence/etc.) - Zoho doesn't need to parse or display
    any of that; a human checks the outcome in this portal's own UI. Keeping
    the contract this thin means Zoho's side never has to change even if the
    result shape here does.

    Upserts by zoho_ticket_id: the first time a ticket id is seen, it's
    created and classified as before. Every call after that (an edit, or a
    retried "On Add" after a slow/cold-start response) updates that same
    row's raw_payload/zoho_category/zoho_subcategory/original_text in place
    instead of creating a second row - and deliberately never touches
    category_id/confidence/reasoning/status, so an unrelated status change
    in Zoho can never silently undo a human's correction in this portal.
    """
    zoho_ticket_id = str(payload.get("zoho_ticket_id") or "").strip()
    if not zoho_ticket_id:
        raise HTTPException(400, "zoho_ticket_id is required")
    issue_text = str(payload.get("issue_in_detail") or "").strip()
    if not issue_text:
        raise HTTPException(400, "issue_in_detail is required")

    zoho_category = str(payload.get("category_of_the_issue") or "").strip() or None
    zoho_subcategory = str(payload.get("sub_category_of_the_issue") or "").strip() or None
    raw_payload = json.dumps(payload)

    existing = db.get_ticket_by_zoho_id(zoho_ticket_id)
    if existing:
        db.update_ticket(
            existing["id"],
            original_text=issue_text,
            full_context=issue_text,
            zoho_category=zoho_category,
            zoho_subcategory=zoho_subcategory,
            raw_payload=raw_payload,
        )
        return {"ok": True, "updated": True, "ticket_id": existing["id"]}

    if not config.OPENAI_API_KEY:
        raise HTTPException(
            500,
            "OPENAI_API_KEY is not set in the server's .env - required for "
            "webhook-triggered classification since there's no UI operator "
            "to supply a per-request key.",
        )

    ticket_id = db.create_ticket(
        issue_text,
        zoho_ticket_id=zoho_ticket_id,
        zoho_category=zoho_category,
        zoho_subcategory=zoho_subcategory,
        raw_payload=raw_payload,
    )
    _run_classification_and_persist(ticket_id, issue_text, clarification_turns=0, api_key=config.OPENAI_API_KEY)

    return {"ok": True}


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


def _to_state_response(ticket: dict) -> TicketStateResponse:
    leaf = taxonomy.get(ticket["category_id"]) if ticket.get("category_id") else None
    conversation = []
    for t in db.get_turns(ticket["id"]):
        conversation.append({"role": t["role"], "content": t["content"]})
    category_name = leaf["name"] if leaf else None
    category_group_name = leaf["parent_name"] if leaf else None
    zoho_category = ticket.get("zoho_category")
    zoho_subcategory = ticket.get("zoho_subcategory")
    zoho_agrees = _labels_loosely_match(category_name, zoho_subcategory)
    if zoho_agrees is None:
        zoho_agrees = _labels_loosely_match(category_group_name, zoho_category)
    raw_payload = json.loads(ticket["raw_payload"]) if ticket.get("raw_payload") else None
    return TicketStateResponse(
        ticket_id=ticket["id"],
        status=ticket["status"],
        category_id=ticket.get("category_id"),
        category_name=category_name,
        category_group_id=leaf["parent_id"] if leaf else None,
        category_group_name=category_group_name,
        assigned_team=leaf.get("assigned_team") if leaf else None,
        poc_primary=leaf.get("poc_primary") if leaf else None,
        poc_cc=leaf.get("poc_cc") if leaf else None,
        confidence=ticket.get("confidence"),
        reasoning=ticket.get("reasoning"),
        clarifying_question=ticket.get("clarifying_question"),
        conversation=conversation,
        zoho_ticket_id=ticket.get("zoho_ticket_id"),
        issue_in_detail=ticket.get("original_text"),
        zoho_category=zoho_category,
        zoho_subcategory=zoho_subcategory,
        zoho_agrees=zoho_agrees,
        raw_payload=raw_payload,
    )


def _run_classification_and_persist(ticket_id: str, context_text: str, clarification_turns: int, api_key: str):
    result = classifier.classify(context_text, api_key)

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
            )
        else:
            db.update_ticket(
                ticket_id,
                status="classified",
                category_id=result.category_id,
                confidence=result.confidence,
                reasoning=result.reasoning,
                full_context=context_text,
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
        )
        db.append_turn(ticket_id, "system_question", result.clarifying_question or "Could you provide more detail?")

    return result


@app.post("/api/tickets", response_model=TicketStateResponse)
def create_ticket(req: NewTicketRequest, api_key: str = Depends(require_api_key), user: str = Depends(require_login)):
    if not req.text.strip():
        raise HTTPException(400, "text is required")
    ticket_id = db.create_ticket(req.text.strip())
    _run_classification_and_persist(ticket_id, req.text.strip(), clarification_turns=0, api_key=api_key)
    ticket = db.get_ticket(ticket_id)

    resp = _to_state_response(ticket)
    if ticket["status"] == "awaiting_clarification":
        last_q = db.get_turns(ticket_id)[-1]["content"]
        resp.clarifying_question = last_q
    return resp


@app.post("/api/tickets/{ticket_id}/respond", response_model=TicketStateResponse)
def respond_to_clarification(
    ticket_id: str, req: ClarificationResponse, api_key: str = Depends(require_api_key), user: str = Depends(require_login)
):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "ticket not found")
    if ticket["status"] != "awaiting_clarification":
        raise HTTPException(400, f"ticket is not awaiting clarification (status={ticket['status']})")

    db.append_turn(ticket_id, "user_answer", req.answer.strip())
    new_context = ticket["full_context"] + f"\n\nAdditional info: {req.answer.strip()}"

    _run_classification_and_persist(ticket_id, new_context, clarification_turns=ticket["clarification_turns"], api_key=api_key)
    updated = db.get_ticket(ticket_id)

    resp = _to_state_response(updated)
    if updated["status"] == "awaiting_clarification":
        last_q = db.get_turns(ticket_id)[-1]["content"]
        resp.clarifying_question = last_q
    return resp


@app.get("/api/tickets")
def list_daily_tickets(date: str | None = None, user: str = Depends(require_login)):
    """
    Day-wise dashboard feed: every ticket created on `date` (YYYY-MM-DD,
    UTC calendar day), defaulting to today, newest first. This is what lets
    the portal show "today's tickets so far" on open/reload without anyone
    having to look each one up manually.
    """
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tickets = db.list_tickets_for_date(date)
    return [_to_state_response(t) for t in tickets]


@app.get("/api/tickets/export.csv")
def export_tickets_csv(date: str | None = None, user: str = Depends(require_login)):
    """
    CSV export for offline/analytical use: ticket text, our predicted
    category/sub-category and confidence, and whatever category Zoho already
    had on the record for comparison. Exports every ticket ever stored when
    `date` is omitted, or just one UTC calendar day when given.
    """
    tickets = db.list_tickets_for_date(date) if date else db.list_all_tickets()
    tickets = sorted(tickets, key=lambda t: t["created_at"])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ticket_id", "zoho_ticket_id", "created_at_utc", "status",
        "issue_in_detail", "app_category_group", "app_subcategory", "confidence",
        "reasoning", "zoho_category", "zoho_subcategory", "matches_zoho_tag",
    ])
    for t in tickets:
        resp = _to_state_response(t)
        writer.writerow([
            resp.ticket_id,
            resp.zoho_ticket_id or "",
            datetime.fromtimestamp(t["created_at"], tz=timezone.utc).isoformat(),
            resp.status,
            resp.issue_in_detail or "",
            resp.category_group_name or "",
            resp.category_name or "",
            resp.confidence if resp.confidence is not None else "",
            resp.reasoning or "",
            resp.zoho_category or "",
            resp.zoho_subcategory or "",
            "" if resp.zoho_agrees is None else ("yes" if resp.zoho_agrees else "no"),
        ])

    filename = f"tickets_{date}.csv" if date else "tickets_all.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/tickets/{ticket_id}", response_model=TicketStateResponse)
def get_ticket(ticket_id: str, user: str = Depends(require_login)):
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "ticket not found")
    return _to_state_response(ticket)


@app.post("/api/tickets/{ticket_id}/correct", response_model=TicketStateResponse)
def correct_ticket(
    ticket_id: str, req: CorrectionRequest, api_key: str = Depends(require_api_key), user: str = Depends(require_login)
):
    """
    Human-in-the-loop correction. This is the endpoint that makes the
    system 'dynamic': the corrected (text -> category) pair is embedded
    and written into vector memory immediately, so the very next similar
    ticket benefits from it without any retraining or redeploy.
    """
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(404, "ticket not found")
    if req.corrected_category_id not in taxonomy.category_ids:
        raise HTTPException(400, f"unknown category_id '{req.corrected_category_id}'")

    db.log_correction(ticket_id, ticket.get("category_id"), req.corrected_category_id, req.corrected_by)
    memory.add_example(ticket["full_context"], req.corrected_category_id, api_key, source="correction")

    db.update_ticket(
        ticket_id,
        status="corrected",
        category_id=req.corrected_category_id,
        confidence=1.0,
        reasoning="Corrected by human reviewer.",
    )
    updated = db.get_ticket(ticket_id)
    return _to_state_response(updated)
