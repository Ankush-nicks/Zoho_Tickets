"""
Resolution quality grading - powers the Stats tab's "Weekly Resolution
Insights" view. Every ticket closed as "Resolved By POC" or "Resolution
Acknowledged" gets graded once (accuracy/completeness/tone/timeliness,
1-5 each) against its own resolution text, and the score is cached on the
ticket (resolution_score* columns) so it's never re-graded.

Same rubric a human QA reviewer would use, run by an LLM instead - AI-
assisted, not a substitute for spot-checking. Tickets with no resolution/
worklog/acknowledgment text on record are auto-scored low (1.0 across the
board) without an API call, rather than skipped, since "closed with
nothing written down" is itself the failure being measured.
"""
import json
import time
from datetime import datetime, timedelta, timezone

from openai import OpenAI

from . import config
from .models import ResolutionGrade

CLOSED_STATUSES = {"Resolved By POC", "Resolution Acknowledged"}
NO_TEXT_GRADE = {
    "accuracy": 1.0, "completeness": 1.0, "tone": 1.0, "timeliness": 1.0,
    "evidence": "Closed with no resolution, worklog, or acknowledgment text on record.",
}
IST = timezone(timedelta(hours=5, minutes=30))


def is_closed(ticket: dict) -> bool:
    status = (ticket.get("raw_payload") or {}).get("ticket_status")
    return status in CLOSED_STATUSES


def resolution_text(ticket: dict) -> str | None:
    """Resolution text if logged, else worklog, else acknowledgment - the
    first one actually written, matching what an instructor would have
    seen. None if the POC left nothing at all."""
    rp = ticket.get("raw_payload") or {}
    for key in ("resolution_by_the_poc", "worklog_from_the_poc", "acknowledgement_from_the_poc"):
        text = (rp.get(key) or "").strip()
        if text:
            return text
    return None


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
    closure timestamp is parseable - passed into the grading prompt so
    Timeliness is judged against a real number, not guessed from tone."""
    closed_at = _parse_zoho_datetime((ticket.get("raw_payload") or {}).get("ticket_closure_date_time"))
    created_at = ticket.get("created_at")
    if closed_at is None or not created_at:
        return None
    return max(0.0, (closed_at - created_at) / 86400)


def _response_schema() -> dict:
    return {
        "name": "resolution_grade",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "accuracy": {"type": "number", "description": "1-5: did the resolution actually address the reported issue?"},
                "completeness": {"type": "number", "description": "1-5: does it cover every part of a multi-part issue, with a real explanation of the fix (not just 'resolved')?"},
                "tone": {"type": "number", "description": "1-5: professional, personalized, respectful of the instructor's time?"},
                "timeliness": {"type": "number", "description": "1-5: reasonable turnaround given the resolution_duration_days provided, if any."},
                "evidence": {"type": "string", "description": "One blunt sentence critiquing (or praising) this specific resolution - concrete, quotable, references what was actually said or missing."},
            },
            "required": ["accuracy", "completeness", "tone", "timeliness", "evidence"],
            "additionalProperties": False,
        },
    }


def _build_prompt(issue_text: str, resolution: str, duration_days: float | None) -> str:
    duration_line = (
        f"{duration_days:.1f} days from raised to closed"
        if duration_days is not None else "unknown (no parseable closure timestamp)"
    )
    return f"""You are grading a support ticket resolution against a QA rubric, the
same one a human reviewer would use. Score each criterion 1-5 (5 = excellent,
1 = failing). Be blunt and specific in "evidence" - it's shown directly to
the team as a coaching example, so reference what was actually said or left
out rather than restating the score in words.

ORIGINAL ISSUE:
{issue_text}

RESOLUTION / WORKLOG / ACKNOWLEDGMENT TEXT (what the POC actually wrote back):
{resolution}

RESOLUTION DURATION: {duration_line}

CRITERIA:
- accuracy: does this actually address the reported problem, or is it generic/off-topic/wrong?
- completeness: does it cover every part of the issue (multi-part issues answered only partially score low), and explain the actual fix rather than just declaring it resolved?
- tone: professional, personalized, and respectful of the instructor's time - not a copy-pasted template.
- timeliness: judge against the duration above - same-day is excellent, multi-week silence is poor, and near-zero-effort tickets closed a long delay after only trivial content still score low here.
"""


def grade_resolution(issue_text: str, resolution: str, duration_days: float | None, api_key: str) -> ResolutionGrade:
    client = OpenAI(api_key=api_key)
    completion = client.chat.completions.create(
        model=config.CLASSIFY_MODEL,
        messages=[{"role": "user", "content": _build_prompt(issue_text, resolution, duration_days)}],
        response_format={"type": "json_schema", "json_schema": _response_schema()},
        temperature=0,
    )
    raw = json.loads(completion.choices[0].message.content)
    return ResolutionGrade(**raw)


def score_ticket(ticket: dict, api_key: str) -> dict:
    """
    Grades one closed ticket and returns the field dict ready to pass to
    db.update_ticket(ticket_id, **result). Raises whatever grade_resolution
    raises (e.g. RateLimitError) when there's text to actually grade -
    callers decide whether to stop a batch on that.
    """
    text = resolution_text(ticket)
    issue = (ticket.get("full_context") or ticket.get("original_text") or "").strip()
    duration = resolution_duration_days(ticket)

    if text is None:
        grade = ResolutionGrade(**NO_TEXT_GRADE)
    else:
        grade = grade_resolution(issue, text, duration, api_key)

    overall = round((grade.accuracy + grade.completeness + grade.tone + grade.timeliness) / 4, 2)
    return {
        "resolution_score": overall,
        "resolution_accuracy": grade.accuracy,
        "resolution_completeness": grade.completeness,
        "resolution_tone": grade.tone,
        "resolution_timeliness": grade.timeliness,
        "resolution_evidence": grade.evidence,
        "resolution_scored_at": time.time(),
    }
