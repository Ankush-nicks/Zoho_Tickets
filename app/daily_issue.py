"""
Backend for the "Daily Issue Check" tab (app/static/daily-issue-check.html):
ticket rows from the portal's own database, and the AI calls the page makes
(summaries, splitting tickets into groups, spotting new issues, questions) -
sent to OpenRouter server-side so the API key never reaches the browser.

The page was built as a standalone artifact that read a CSV export; ticket
rows keep that export's compact array shape so the page's own code works
unchanged (see ticket_row / the page's mk()).
"""
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from openai import (
    APIConnectionError, APIStatusError, AuthenticationError, BadRequestError,
    OpenAI, PermissionDeniedError, RateLimitError,
)

from . import config
from .taxonomy import taxonomy

IST = timezone(timedelta(hours=5, minutes=30))

# Same caps the page applied when it imported a CSV itself.
_TEXT_MAX = 900
_RESOLUTION_MAX = 400

# The page builds its own prompts from up to ~220 tickets; this only stops
# a runaway request well past what gpt-4o-mini's context could take anyway.
PROMPT_MAX_CHARS = 400_000

# Appended after a streamed answer: END + "truncated" or END + "error" -
# the page strips it (see aiText in daily-issue-check.html).
STREAM_END = "\u0003"


def _clean(v, n: int | None = None) -> str:
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    return v[:n] if n else v


def _zoho_dt_to_iso(s) -> str:
    """Zoho's 'dd/mm/yyyy HH:MM:SS' (IST wall clock) -> 'yyyy-mm-ddTHH:MM:SS', or ''."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$", str(s or ""))
    if not m:
        return ""
    dd, mm, yyyy, hh, mi, ss = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}T{int(hh):02d}:{mi}:{ss or '00'}"


def ticket_row(t: dict) -> list | None:
    """
    One ticket in the page's row shape:
    [id, status, category, subcategory, subject, university, raised (IST,
     ISO), sla, recurring, session type, issue text, resolution,
     reopen count, assigned team, closed (IST, ISO)].

    Category is our taxonomy leaf when the ticket has one (it reflects any
    correction made in the portal), else whatever Zoho had. Tickets without
    a numeric Zoho Ticket ID (manual test tickets) are left out - the page
    keys everything on that number.
    """
    zoho_id = str(t.get("zoho_ticket_id") or "").strip()
    if not zoho_id.isdigit():
        return None
    raw = t.get("raw_payload") or {}
    leaf = taxonomy.get(t["category_id"]) if t.get("category_id") else None
    category = leaf["parent_name"] if leaf else (t.get("zoho_category") or raw.get("category_of_the_issue"))
    subcategory = leaf["name"] if leaf else (t.get("zoho_subcategory") or raw.get("sub_category_of_the_issue"))
    if not category:
        return None
    try:
        reopened = int(float(raw.get("ticket_reopen_count") or 0))
    except (TypeError, ValueError):
        reopened = 0
    raised = datetime.fromtimestamp(t["created_at"], IST).strftime("%Y-%m-%dT%H:%M:%S")
    return [
        int(zoho_id),
        _clean(raw.get("ticket_status")),
        _clean(category),
        _clean(subcategory),
        _clean(raw.get("subject_name")),
        _clean(raw.get("university_boa") or raw.get("university")),
        raised,
        _clean(raw.get("sla_breach_status")),
        _clean(raw.get("is_it_a_recurring_issue")),
        _clean(raw.get("session_type")),
        _clean(t.get("original_text"), _TEXT_MAX),
        _clean(raw.get("resolution_by_the_poc"), _RESOLUTION_MAX),
        reopened,
        _clean(raw.get("assigned_team")),
        _zoho_dt_to_iso(raw.get("ticket_closure_date_time")),
    ]


def build_rows(tickets: list[dict]) -> list[list]:
    """Rows for every ticket the page can show, one per Zoho Ticket ID (the
    most recent stored row wins if an id somehow appears twice)."""
    by_id: dict[int, list] = {}
    for t in tickets:  # oldest first, so later rows overwrite earlier ones
        row = ticket_row(t)
        if row is not None:
            by_id[row[0]] = row
    return list(by_id.values())


# --- AI ------------------------------------------------------------------

def ai_enabled() -> bool:
    return bool(config.OPENROUTER_API_KEY)


def _client() -> OpenAI:
    return OpenAI(api_key=config.OPENROUTER_API_KEY, base_url=config.OPENROUTER_BASE_URL)


def _fail(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def _check_prompt(prompt) -> str:
    if not ai_enabled():
        _fail(503, "not_configured", "OPENROUTER_API_KEY is not set on the server.")
    prompt = str(prompt or "").strip()
    if not prompt:
        _fail(400, "empty_prompt", "prompt is required")
    if len(prompt) > PROMPT_MAX_CHARS:
        _fail(413, "prompt_too_large", "Too much text for one request.")
    return prompt


def _raise_for_openrouter(e: Exception):
    """Map an OpenRouter/OpenAI-client error to the codes the page understands."""
    if isinstance(e, RateLimitError):
        _fail(429, "rate_limited", "OpenRouter rate limit hit.")
    if isinstance(e, (AuthenticationError, PermissionDeniedError)) or (
        isinstance(e, APIStatusError) and e.status_code == 402
    ):
        _fail(402, "no_credits", "OpenRouter key rejected or out of credits.")
    if isinstance(e, BadRequestError) and re.search(r"context|too long|maximum|tokens", str(e), re.I):
        _fail(413, "prompt_too_large", "Too much text for one request.")
    if isinstance(e, (APIStatusError, APIConnectionError)):
        _fail(502, "upstream_error", f"OpenRouter request failed: {str(e)[:200]}")
    raise e


def stream_text(prompt) -> "iter":
    """
    Starts a streamed completion and returns a generator of text chunks.
    The request itself is made here, before any byte is streamed, so an
    error (rate limit, no credits, ...) still becomes a proper HTTP error
    status instead of a half-written 200 response.
    """
    prompt = _check_prompt(prompt)
    try:
        stream = _client().chat.completions.create(
            model=config.DAILY_ISSUE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1800,
            stream=True,
        )
    except Exception as e:
        _raise_for_openrouter(e)

    def gen():
        finish = None
        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    yield choice.delta.content
                if choice.finish_reason:
                    finish = choice.finish_reason
        except Exception:
            yield STREAM_END + "error"
            return
        if finish == "length":
            yield STREAM_END + "truncated"

    return gen()


def complete_json(prompt) -> dict:
    """One JSON-mode completion; the page's prompts all end with 'Reply with
    only JSON ...', which JSON mode requires."""
    prompt = _check_prompt(prompt)
    try:
        completion = _client().chat.completions.create(
            model=config.DAILY_ISSUE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        _raise_for_openrouter(e)
    try:
        data = json.loads(completion.choices[0].message.content or "")
    except (ValueError, IndexError, AttributeError):
        _fail(422, "invalid_json", "The model's answer wasn't valid JSON.")
    if not isinstance(data, dict):
        _fail(422, "invalid_json", "The model's answer wasn't a JSON object.")
    return data
