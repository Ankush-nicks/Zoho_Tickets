import csv
import io
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db, memory, classifier, zoho
from .taxonomy import taxonomy
from .auth import require_login
from .models import (
    LoginRequest,
    NewTicketRequest,
    ClarificationResponse,
    CorrectionRequest,
    TicketStateResponse,
    ZohoWebhookTicket,
)

app = FastAPI(title="Ticket Classifier", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="ticket_router_session")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


@app.get("/zoho-debug")
def zoho_debug_page(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login")
    return FileResponse(str(STATIC_DIR / "zoho_debug.html"))


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

@app.get("/_mock/zoho-invoke")
def _mock_zoho_invoke(ticket_id: str):
    """
    TEMPORARY local stand-in for the real Zoho Creator Invoke URL, so the
    fetch/auth/parse plumbing in app/zoho.py can be exercised end-to-end
    before real Zoho credentials exist. ZOHO_INVOKE_URL defaults to this
    route. Delete this once the real invoke URL is wired up in .env.
    """
    return {
        "code": 3000,
        "result": [
            {
                "ID": "6234000000123456",
                "Ticket_ID": ticket_id,
                "Issue_in_Detail": (
                    f"[sample Zoho data for ticket {ticket_id}] The QA report for last week's "
                    "session shows a zero score on a rubric item I clearly addressed - please review."
                ),
                "Status": "Open",
                "Created_Time": "2026-08-01 10:15:00",
            }
        ],
    }


@app.get("/api/zoho/tickets/{ticket_id}")
def get_zoho_ticket(ticket_id: str, user: str = Depends(require_login)):
    """Fetch a ticket from Zoho Creator and return the fields we extracted plus the raw response."""
    try:
        return zoho.get_ticket_issue(ticket_id)
    except zoho.ZohoError as e:
        raise HTTPException(502, str(e))


@app.get("/api/zoho/status")
def get_zoho_status(user: str = Depends(require_login)):
    """
    Whether Zoho is still pointed at the local sample/mock setup vs a real
    invoke URL and credential - never returns the actual URL or key value,
    just enough to render a status indicator in the UI.
    """
    return {
        "invoke_url_is_sample": "/_mock/zoho-invoke" in config.ZOHO_INVOKE_URL,
        "api_key_is_sample": config.ZOHO_API_KEY == "sample-zoho-key",
    }


@app.post("/api/webhooks/zoho/tickets", response_model=TicketStateResponse)
def webhook_new_zoho_ticket(req: ZohoWebhookTicket, _: None = Depends(require_webhook_secret)):
    """
    PUSH counterpart to the pull-based /api/zoho endpoints above: Zoho
    Creator's "On Add" workflow calls this directly with the new record's
    fields (see zoho-invoke-url-setup.md for the Deluge script), so a new
    ticket gets classified the moment it's created in Zoho - no polling,
    no round trip back through ZOHO_INVOKE_URL.

    Auth is a shared secret (X-Webhook-Secret, see require_webhook_secret)
    rather than the session login the UI uses, since Deluge can't hold a
    browser session. Classification runs with the server-side
    OPENAI_API_KEY (no UI operator is present to supply one per-request).
    """
    if not config.OPENAI_API_KEY:
        raise HTTPException(
            500,
            "OPENAI_API_KEY is not set in the server's .env - required for "
            "webhook-triggered classification since there's no UI operator "
            "to supply a per-request key.",
        )
    issue_text = req.issue_in_detail.strip()
    if not issue_text:
        raise HTTPException(400, "issue_in_detail is required")

    ticket_id = db.create_ticket(issue_text, zoho_ticket_id=req.zoho_ticket_id.strip())
    _run_classification_and_persist(ticket_id, issue_text, clarification_turns=0, api_key=config.OPENAI_API_KEY)
    ticket = db.get_ticket(ticket_id)

    resp = _to_state_response(ticket)
    if ticket["status"] == "awaiting_clarification":
        last_q = db.get_turns(ticket_id)[-1]["content"]
        resp.clarifying_question = last_q
    return resp


def _to_state_response(ticket: dict) -> TicketStateResponse:
    leaf = taxonomy.get(ticket["category_id"]) if ticket.get("category_id") else None
    conversation = []
    for t in db.get_turns(ticket["id"]):
        conversation.append({"role": t["role"], "content": t["content"]})
    return TicketStateResponse(
        ticket_id=ticket["id"],
        status=ticket["status"],
        category_id=ticket.get("category_id"),
        category_name=leaf["name"] if leaf else None,
        category_group_id=leaf["parent_id"] if leaf else None,
        category_group_name=leaf["parent_name"] if leaf else None,
        assigned_team=leaf.get("assigned_team") if leaf else None,
        poc_primary=leaf.get("poc_primary") if leaf else None,
        poc_cc=leaf.get("poc_cc") if leaf else None,
        confidence=ticket.get("confidence"),
        reasoning=ticket.get("reasoning"),
        clarifying_question=ticket.get("clarifying_question"),
        conversation=conversation,
        zoho_ticket_id=ticket.get("zoho_ticket_id"),
        issue_in_detail=ticket.get("original_text"),
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


@app.post("/api/zoho/tickets/{zoho_ticket_id}/classify", response_model=TicketStateResponse)
def classify_zoho_ticket(zoho_ticket_id: str, api_key: str = Depends(require_api_key), user: str = Depends(require_login)):
    """
    Fetch a ticket from Zoho Creator and classify its "Issue in Detail" text
    through the exact same pipeline manual input uses (create_ticket ->
    _run_classification_and_persist -> _to_state_response) - nothing about
    the classification logic itself is different for a Zoho-sourced ticket.
    """
    try:
        zoho_result = zoho.get_ticket_issue(zoho_ticket_id)
    except zoho.ZohoError as e:
        raise HTTPException(502, str(e))

    issue_text = zoho_result.get("issue_in_detail")
    if not issue_text or not str(issue_text).strip():
        raise HTTPException(
            502,
            f"Zoho response for ticket '{zoho_ticket_id}' has no usable "
            f"'{config.ZOHO_FIELD_ISSUE_DETAIL}' field - check ZOHO_FIELD_ISSUE_DETAIL "
            f"or inspect the raw response at /api/zoho/tickets/{zoho_ticket_id}.",
        )
    issue_text = str(issue_text).strip()
    resolved_zoho_id = str(zoho_result.get("ticket_id") or zoho_ticket_id)

    ticket_id = db.create_ticket(issue_text, zoho_ticket_id=resolved_zoho_id)
    _run_classification_and_persist(ticket_id, issue_text, clarification_turns=0, api_key=api_key)
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
