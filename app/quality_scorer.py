"""
Resolution quality grading - powers the Stats tab's "Weekly Resolution
Insights" view. Every ticket closed as "Resolved By POC" or "Resolution
Acknowledged" gets graded once against the 5-parameter Ticket Quality-Check
rubric below, and the score is cached on the ticket (resolution_* columns)
so it's never re-graded.

Each parameter has exactly 3 allowed band values (not a free scale) -
weights sum to 10:
  1. Acknowledgement within 4 hours - max 2
  2. Investigation Done          - max 1.5
  3. Root Cause Fix              - max 2.5
  4. SLA                         - max 2
  5. Resolution (detail)         - max 2

Same rubric a human QA reviewer would use, run by an LLM instead - AI-
assisted, not a substitute for spot-checking. Tickets with no
acknowledgement, worklog, or resolution text on record at all are
auto-scored 0/10 without an API call, rather than skipped, since "closed
with nothing written down" is itself the failure being measured.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from openai import OpenAI, RateLimitError

from . import config
from .models import ResolutionGrade

CLOSED_STATUSES = {"Resolved By POC", "Resolution Acknowledged"}
NO_TEXT_GRADE = {
    "acknowledgement": 0.0, "investigation": 0.0, "root_cause_fix": 0.0, "sla": 0.0, "resolution_detail": 0.0,
    "evidence": "Closed with no acknowledgement, worklog, or resolution text on record.",
}
IST = timezone(timedelta(hours=5, minutes=30))

# (db field name, max weight) - order matches the rubric and drives the
# per-criterion normalization the Insights tab uses (each is graded on its
# own scale, so "weakest criterion" has to compare fraction-of-max, not
# raw score).
CRITERIA = [
    ("resolution_ack", 2.0),
    ("resolution_investigation", 1.5),
    ("resolution_root_cause", 2.5),
    ("resolution_sla", 2.0),
    ("resolution_detail", 2.0),
]


def is_closed(ticket: dict) -> bool:
    status = (ticket.get("raw_payload") or {}).get("ticket_status")
    return status in CLOSED_STATUSES


def _has_any_documentation(rp: dict) -> bool:
    fields = (
        "acknowledgement_from_the_poc", "acknowledgement_history",
        "worklog_from_the_poc", "worklog_history",
        "resolution_by_the_poc",
    )
    return any((rp.get(f) or "").strip() for f in fields)


def _joined(rp: dict, *keys: str) -> str:
    parts = [(rp.get(k) or "").strip() for k in keys]
    return "\n".join(p for p in parts if p)


def _parse_zoho_datetime(s: str | None) -> float | None:
    """Best-effort parse of Zoho's 'DD/MM/YYYY HH:MM:SS' (IST) datetime
    fields (e.g. ticket_closure_date_time) into a UTC epoch. Returns None
    on anything that doesn't match - callers should treat that as
    'duration unknown', not an error."""
    if not s or not s.strip():
        return None
    try:
        dt = datetime.strptime(s.strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
        return dt.astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def resolution_duration_days(ticket: dict) -> float | None:
    """Days between the ticket being raised and closed, when Zoho's own
    closure timestamp is parseable - passed into the grading prompt so SLA
    is judged against a real number, not guessed from tone."""
    closed_at = _parse_zoho_datetime((ticket.get("raw_payload") or {}).get("ticket_closure_date_time"))
    created_at = ticket.get("created_at")
    if closed_at is None or not created_at:
        return None
    return max(0.0, (closed_at - created_at) / 86400)


def _response_schema() -> dict:
    return {
        "name": "ticket_quality_grade",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "acknowledgement": {
                    "type": "number", "enum": [2, 1, 0],
                    "description": "2 = acknowledged within 4 hrs, with a proper message (timeline/owner context). 1 = acknowledged, but late or generic. 0 = no acknowledgement logged at any point.",
                },
                "investigation": {
                    "type": "number", "enum": [1.5, 0.75, 0],
                    "description": "1.5 = worklog shows a clear investigation trail (what was checked, what was found). 0.75 = investigation happened but is hardly logged (one-line note, no detail). 0 = no worklog entry for investigation at all.",
                },
                "root_cause_fix": {
                    "type": "number", "enum": [2.5, 1.25, 0],
                    "description": "2.5 = root cause clearly identified + permanent fix confirmed. 1.25 = root cause identified but fix is a workaround / not confirmed permanent. 0 = no root cause stated, or issue is patched without diagnosis.",
                },
                "sla": {
                    "type": "number", "enum": [2, 1, 0],
                    "description": "2 = resolved within SLA. 1 = SLA breached, but with a valid, documented reason (e.g. awaiting instructor, dependent team). 0 = SLA breached with no explanation logged.",
                },
                "resolution_detail": {
                    "type": "number", "enum": [2, 1, 0],
                    "description": "2 = detailed, specific resolution note - what was actually done, in enough detail that anyone reading it later understands the fix without asking the assignee. 1 = some detail, but vague or partial. 0 = generic note only ('Fixed', 'Resolved', 'Done').",
                },
                "evidence": {
                    "type": "string",
                    "description": "One blunt sentence naming the weakest-scoring parameter and exactly what was/wasn't done or written - concrete and quotable, not a restatement of the score.",
                },
            },
            "required": ["acknowledgement", "investigation", "root_cause_fix", "sla", "resolution_detail", "evidence"],
            "additionalProperties": False,
        },
    }


def _build_prompt(
    issue_text: str, ack_text: str, worklog_text: str, resolution_note: str,
    sla_status: str | None, duration_days: float | None,
) -> str:
    duration_line = (
        f"{duration_days:.1f} days from raised to closed"
        if duration_days is not None else "unknown (no parseable closure timestamp)"
    )
    return f"""You are grading a closed support ticket against a strict 5-parameter
QA rubric. Score EACH parameter using ONLY one of its exact listed band
values - never an in-between number. When the text below doesn't give you
enough to judge a parameter confidently, default to the LOWER band rather
than guessing generously.

ORIGINAL ISSUE:
{issue_text}

ACKNOWLEDGEMENT LOGGED (POC's ack message / history, if any):
{ack_text or '(none logged)'}

WORKLOG (investigation trail, if any):
{worklog_text or '(none logged)'}

RESOLUTION NOTE (what the POC wrote as the fix, if any):
{resolution_note or '(none logged)'}

SLA STATUS (as recorded by Zoho): {sla_status or 'not recorded'}
TICKET DURATION: {duration_line}

PARAMETERS (score each with EXACTLY one of the listed values):

1. Acknowledgement within 4 hours - max 2
   2 = acknowledged within 4 hrs, with a proper message (timeline/owner context)
   1 = acknowledged, but late or generic
   0 = no acknowledgement logged at any point

2. Investigation Done - max 1.5
   1.5 = worklog shows a clear investigation trail (what was checked, what was found)
   0.75 = investigation happened but is hardly logged (one-line note, no detail)
   0 = no worklog entry for investigation at all

3. Root Cause Fix - max 2.5
   2.5 = root cause clearly identified + permanent fix confirmed
   1.25 = root cause identified but fix is a workaround / not confirmed permanent
   0 = no root cause stated, or issue is patched without diagnosis

4. SLA - max 2
   2 = resolved within SLA
   1 = SLA breached, but with a valid, documented reason (e.g. awaiting instructor, dependent team)
   0 = SLA breached with no explanation logged

5. Resolution - max 2
   2 = detailed, specific resolution note - what was actually done, in enough detail that anyone reading it later understands the fix without asking the assignee
   1 = some detail, but vague or partial
   0 = generic note only ("Fixed", "Resolved", "Done")
"""


def grade_resolution(
    issue_text: str, ack_text: str, worklog_text: str, resolution_note: str,
    sla_status: str | None, duration_days: float | None, api_key: str,
) -> ResolutionGrade:
    """
    api_key is an OpenRouter key - grading has no embedding step, so unlike
    classify() there's no separate OpenAI key needed here.

    Falls back to Cloudflare Workers AI (see classifier._classify_via_
    cloudflare's sibling below) on a RateLimitError, same as classify() - a
    separate quota from OpenRouter's, so grading keeps working instead of
    stalling. Re-raises as before when Cloudflare isn't set up.
    """
    prompt = _build_prompt(issue_text, ack_text, worklog_text, resolution_note, sla_status, duration_days)
    client = OpenAI(api_key=api_key, base_url=config.OPENROUTER_BASE_URL)
    try:
        completion = client.chat.completions.create(
            model=config.OPENROUTER_CLASSIFY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": _response_schema()},
            temperature=0,
        )
    except RateLimitError:
        if not (config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_API_TOKEN):
            raise
        return _grade_via_cloudflare(prompt)
    raw = json.loads(completion.choices[0].message.content)
    return ResolutionGrade(**raw)


def _extract_cloudflare_json(data: dict) -> dict:
    """
    Workers AI's structured output isn't 100% guaranteed to land as a
    parsed object - on some inputs result.response comes back as a raw
    JSON string that still needs a json.loads, or is missing entirely
    with the JSON sitting in the plain chat message content instead.
    """
    result = data.get("result") or {}
    response = result.get("response")
    if isinstance(response, dict):
        return response
    if isinstance(response, str) and response.strip():
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            pass
    content = (((result.get("choices") or [{}])[0]).get("message") or {}).get("content")
    if content:
        return json.loads(content)
    raise RuntimeError(f"Cloudflare Workers AI returned no parseable structured output: {data}")


def _grade_via_cloudflare(prompt: str) -> ResolutionGrade:
    import httpx

    url = f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/run/{config.CLOUDFLARE_WORKERS_AI_MODEL}"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}"},
        json={
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": _response_schema()["schema"]},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Cloudflare Workers AI error: {data.get('errors')}")
    return ResolutionGrade(**_extract_cloudflare_json(data))


def score_ticket(ticket: dict, api_key: str) -> dict:
    """
    Grades one closed ticket against the 5-parameter rubric and returns the
    field dict ready to pass to db.update_ticket(ticket_id, **result).
    Raises whatever grade_resolution raises (e.g. RateLimitError) when
    there's text to actually grade - callers decide whether to stop a
    batch on that.
    """
    rp = ticket.get("raw_payload") or {}
    issue = (ticket.get("full_context") or ticket.get("original_text") or "").strip()
    duration = resolution_duration_days(ticket)

    if not _has_any_documentation(rp):
        grade = ResolutionGrade(**NO_TEXT_GRADE)
    else:
        ack_text = _joined(rp, "acknowledgement_from_the_poc", "acknowledgement_history")
        worklog_text = _joined(rp, "worklog_from_the_poc", "worklog_history")
        resolution_note = (rp.get("resolution_by_the_poc") or "").strip()
        grade = grade_resolution(issue, ack_text, worklog_text, resolution_note, rp.get("sla_breach_status"), duration, api_key)

    total = round(
        grade.acknowledgement + grade.investigation + grade.root_cause_fix + grade.sla + grade.resolution_detail, 2
    )
    return {
        "resolution_score": total,  # out of 10
        "resolution_ack": grade.acknowledgement,
        "resolution_investigation": grade.investigation,
        "resolution_root_cause": grade.root_cause_fix,
        "resolution_sla": grade.sla,
        "resolution_detail": grade.resolution_detail,
        "resolution_evidence": grade.evidence,
        "resolution_scored_at": time.time(),
    }
