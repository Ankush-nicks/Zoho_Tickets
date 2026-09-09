"""
Read-only ticket queue for a single POC (Point of Contact) - backs the
GET /api/extension/my-tickets endpoint consumed by that POC's Chrome
extension. See docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md
for the full design and the reasoning behind every decision below.

Deliberately has no write path: nothing here ever calls db.update_ticket.
"""
import re
import secrets
import time

from . import config, db
from .quality_scorer import CLOSED_STATUSES
from .taxonomy import taxonomy

ACK_ACKNOWLEDGED = "acknowledged"
ACK_PENDING = "pending"
ACK_MISSED = "missed"

SLA_ON_TRACK = "on_track"
SLA_AT_RISK = "at_risk"
SLA_BREACHED = "breached"

OPEN_STATUSES = ("classified", "corrected")


def resolve_poc_email(token: str | None) -> str | None:
    """
    Reverse-lookup config.POC_TOKENS: the presented X-POC-Token header value
    -> the POC email it authenticates as, or None if it's missing, empty, or
    doesn't match any configured token. Constant-time comparison per token
    (like require_webhook_secret), not a plain dict lookup by value, since
    token values are secrets.
    """
    if not token:
        return None
    for email, configured_token in config.POC_TOKENS.items():
        if configured_token and secrets.compare_digest(token, configured_token):
            return email
    return None


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def _poc_matches(poc_primary_field: str | None, poc_email: str) -> bool:
    """
    True if poc_email appears as one of the email addresses embedded in
    poc_primary_field (case-insensitive). taxonomy.json's poc_primary is
    free text - sometimes one clean email, sometimes several comma-separated,
    sometimes prose with an email buried inside it (e.g. "Respective COS's >
    If chose 'Back up required' > arunkumar.naram@nxtwave.co.in"), sometimes
    a prose placeholder with no email at all ("Respective Capability
    Manager"). A naive comma-split-and-exact-match fails on the prose cases
    above because the comma/semicolon-delimited segment containing the email
    also contains other text. Extracting every email-shaped substring and
    checking containment against that set handles all of these uniformly,
    including the "no email at all" case (extracts zero emails, so it never
    matches).
    """
    if not poc_primary_field:
        return False
    emails = {m.group(0).casefold() for m in _EMAIL_RE.finditer(poc_primary_field)}
    return poc_email.strip().casefold() in emails


def _is_closed(raw_payload: dict | None) -> bool:
    """
    True only when raw_payload carries a ticket_status Zoho considers closed
    (the same CLOSED_STATUSES set quality_scorer.py already uses for
    resolution grading - one definition of "closed", not two). A ticket with
    no ticket_status at all (never came from Zoho, or Zoho hasn't sent a
    status yet) is always treated as open.

    Known gap: Zoho's real ticket_status values also include "Closed" (see
    app/static/index.html's broader PULSE_CLOSED_STATUSES, which adds
    "Closed" on top of this same quality_scorer.CLOSED_STATUSES set), which
    is NOT in quality_scorer.CLOSED_STATUSES - tickets marked "Closed" in
    Zoho will not be excluded here. ("Discard" is a separate Zoho status
    that Pulse itself deliberately treats as still-open, not closed - see
    PULSE_CLOSED_STATUSES's usage in index.html - so it is not a gap
    relative to the rest of this app, just something to be aware of if that
    ever changes.) This module deliberately reuses the existing
    CLOSED_STATUSES definition rather than creating a second one;
    broadening it is a decision for whoever owns that shared constant, not
    this feature.
    """
    if not raw_payload:
        return False
    status = raw_payload.get("ticket_status")
    return bool(status) and status in CLOSED_STATUSES


def _compute_ack(created_at: float, acknowledged_at: float | None, now: float) -> dict:
    deadline = created_at + config.ACK_WINDOW_HOURS * 3600
    if acknowledged_at is not None:
        return {
            "ack_deadline_at": deadline,
            "acknowledged_at": acknowledged_at,
            "ack_state": ACK_ACKNOWLEDGED,
            "ack_urgent": False,
        }
    remaining = deadline - now
    if remaining <= 0:
        return {
            "ack_deadline_at": deadline,
            "acknowledged_at": None,
            "ack_state": ACK_MISSED,
            "ack_urgent": False,
        }
    return {
        "ack_deadline_at": deadline,
        "acknowledged_at": None,
        "ack_state": ACK_PENDING,
        "ack_urgent": remaining <= config.ACK_URGENT_MINUTES * 60,
    }


def _sla_hours_for_group(group_code: str) -> float:
    return config.CATEGORY_SLA_HOURS.get(group_code, config.CATEGORY_SLA_HOURS_DEFAULT)


def _compute_sla(created_at: float, group_code: str, now: float) -> dict:
    hours = _sla_hours_for_group(group_code)
    window_seconds = hours * 3600
    deadline = created_at + window_seconds
    remaining = deadline - now

    out = {"sla_hours": hours, "sla_deadline_at": deadline}
    if remaining <= 0:
        out["sla_state"] = SLA_BREACHED
        out["sla_overdue_seconds"] = -remaining
    elif remaining <= window_seconds * config.SLA_AT_RISK_FRACTION:
        out["sla_state"] = SLA_AT_RISK
    else:
        out["sla_state"] = SLA_ON_TRACK
    return out


def build_poc_queue(poc_email: str, now: float | None = None) -> dict:
    """
    Every open ticket routed to poc_email, with ack/SLA state resolved as of
    `now` (defaults to the real current time - overridable so tests can pin
    a fixed clock instead of racing real time). See module docstring and the
    design doc for the selection rules.
    """
    if now is None:
        now = time.time()

    tickets_out = []
    for t in db.list_all_tickets():
        if t["status"] not in OPEN_STATUSES:
            continue
        category_id = t.get("category_id")
        if not category_id:
            continue
        leaf = taxonomy.get(category_id)
        if not leaf:
            continue
        if not _poc_matches(leaf.get("poc_primary"), poc_email):
            continue
        if _is_closed(t.get("raw_payload")):
            continue

        raw_payload = t.get("raw_payload") or {}
        issue_text = (t.get("full_context") or t.get("original_text") or "").strip()
        entry = {
            "id": t["id"],
            "zoho_ticket_id": t.get("zoho_ticket_id"),
            "category_group_code": leaf["parent_id"],
            "category_group_name": leaf["parent_name"],
            "assigned_team": leaf.get("assigned_team"),
            "issue_summary": issue_text[:200],
            "created_at": t["created_at"],
            **_compute_ack(t["created_at"], t.get("acknowledged_at"), now),
            **_compute_sla(t["created_at"], leaf["parent_id"], now),
        }
        priority = raw_payload.get("priority_level")
        if priority:
            entry["priority"] = priority
        tickets_out.append(entry)

    summary = {
        "breached": sum(1 for x in tickets_out if x["sla_state"] == SLA_BREACHED),
        "needs_ack_now": sum(
            1 for x in tickets_out if x["ack_state"] == ACK_MISSED or x["ack_urgent"]
        ),
        "on_track": sum(1 for x in tickets_out if x["sla_state"] == SLA_ON_TRACK),
    }
    return {"tickets": tickets_out, "summary": summary}
