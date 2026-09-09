import pytest

from app import config, db, poc_queue

POC_EMAIL = "ranjith.kumar@nxtwave.co.in"


def _make_ticket(created_at, category_id="G01-S01", status="classified",
                  raw_payload=None, acknowledged_at=None, zoho_ticket_id=None):
    ticket_id = db.create_ticket(
        "some issue text", zoho_ticket_id=zoho_ticket_id, raw_payload=raw_payload, created_at=created_at,
    )
    db.update_ticket(ticket_id, status=status, category_id=category_id, acknowledged_at=acknowledged_at)
    return ticket_id


def test_ticket_routed_to_this_poc_is_included(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert len(result["tickets"]) == 1
    assert result["tickets"][0]["category_group_code"] == "G01"
    assert result["tickets"][0]["category_group_name"] == "QA Report / Instructor Evaluation"


def test_ticket_routed_to_a_different_poc_is_excluded(isolated_db):
    now = 1_000_000.0
    # G04-S01 routes to narayana.dubbala/gompa.mounica, not ranjith
    _make_ticket(created_at=now - 3600, category_id="G04-S01")

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["tickets"] == []


def test_comma_separated_poc_primary_matches_by_containment(isolated_db):
    now = 1_000_000.0
    # G03-S01's poc_primary is "catherine..., gauthami..., ankon..." (comma list)
    _make_ticket(created_at=now - 3600, category_id="G03-S01")

    result = poc_queue.build_poc_queue("catherine.joannamathews@nxtwave.co.in", now=now)

    assert len(result["tickets"]) == 1


def test_email_embedded_in_prose_poc_primary_matches_by_containment(isolated_db):
    now = 1_000_000.0
    # G03-S04's poc_primary is "Respective Capability Manager; escalate to
    # catherine..., gauthami..., ankon..." - a naive comma-split leaves the
    # first email buried in a longer prose segment.
    _make_ticket(created_at=now - 3600, category_id="G03-S04")

    result = poc_queue.build_poc_queue("gauthami.chandil@nxtwave.co.in", now=now)

    assert len(result["tickets"]) == 1


def test_email_embedded_in_arrow_separated_prose_matches_by_containment(isolated_db):
    now = 1_000_000.0
    # G06-S01's poc_primary is "Respective COS's > If chose 'Back up
    # required' > arunkumar.naram@nxtwave.co.in".
    _make_ticket(created_at=now - 3600, category_id="G06-S01")

    result = poc_queue.build_poc_queue("arunkumar.naram@nxtwave.co.in", now=now)

    assert len(result["tickets"]) == 1


def test_poc_primary_with_no_email_never_matches(isolated_db):
    now = 1_000_000.0
    # G09-S01's poc_primary is "Respective Capability Manager" - no email at
    # all, so it should never match any poc_email.
    _make_ticket(created_at=now - 3600, category_id="G09-S01")

    result = poc_queue.build_poc_queue("anyone@nxtwave.co.in", now=now)

    assert result["tickets"] == []


def test_needs_human_review_ticket_is_excluded_regardless_of_category(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, status="needs_human_review")

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["tickets"] == []


def test_closed_zoho_ticket_is_excluded(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, raw_payload={"ticket_status": "Resolved By POC"})

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["tickets"] == []


def test_ticket_with_no_ticket_status_is_treated_as_open(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, raw_payload={"some_other_field": "x"})

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert len(result["tickets"]) == 1


def test_ack_state_pending_when_within_window(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)  # 1h old, 4h ack window

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "pending"
    assert ticket["ack_urgent"] is False


def test_ack_state_urgent_in_last_15_minutes(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - (4 * 3600 - 10 * 60))  # 10 min left of the 4h window

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "pending"
    assert ticket["ack_urgent"] is True


def test_ack_state_missed_after_window_passes(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - (4 * 3600 + 60))  # 1 minute past the 4h window

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "missed"


def test_ack_state_acknowledged_when_timestamp_set(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - (4 * 3600 + 60), acknowledged_at=now - 3600)

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "acknowledged"
    assert ticket["acknowledged_at"] == now - 3600


def test_sla_on_track_well_within_window(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)  # 1h of 24h - nowhere near the last 15%

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["sla_hours"] == 24.0
    assert ticket["sla_state"] == "on_track"


def test_sla_at_risk_in_last_15_percent_of_window(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    # 24h window, 15% = 3.6h; 2h remaining is inside that
    _make_ticket(created_at=now - (24 * 3600 - 2 * 3600))

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["sla_state"] == "at_risk"


def test_sla_breached_past_deadline_reports_overdue_seconds(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    _make_ticket(created_at=now - (24 * 3600 + 3600))  # 1h past the 24h deadline

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["sla_state"] == "breached"
    assert ticket["sla_overdue_seconds"] == pytest.approx(3600.0)


def test_unmapped_group_falls_back_to_default_sla_hours(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "CATEGORY_SLA_HOURS_DEFAULT", 48.0)
    now = 1_000_000.0
    # G09-S04 "Skip Level Escalation (General)" isn't in CATEGORY_SLA_HOURS
    ticket_id = db.create_ticket("some issue text", created_at=now - 3600)
    db.update_ticket(ticket_id, status="classified", category_id="G09-S04")

    result = poc_queue.build_poc_queue("prudhviraj.garlapati@nxtwave.co.in", now=now)

    assert result["tickets"][0]["sla_hours"] == 48.0


def test_priority_passed_through_from_raw_payload_when_present(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, raw_payload={"priority_level": "P1"})

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["priority"] == "P1"


def test_priority_omitted_when_zoho_sends_none(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert "priority" not in ticket


def test_summary_counts_breached_needs_ack_now_and_on_track(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)                # on track, ack pending, not urgent
    _make_ticket(created_at=now - (24 * 3600 + 3600))  # SLA breached, ack missed too

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["summary"] == {"breached": 1, "needs_ack_now": 1, "on_track": 1}


def test_resolve_poc_email_with_valid_token(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")

    assert poc_queue.resolve_poc_email("secret-token") == POC_EMAIL


def test_resolve_poc_email_with_unknown_or_missing_token(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")

    assert poc_queue.resolve_poc_email("wrong-token") is None
    assert poc_queue.resolve_poc_email(None) is None
    assert poc_queue.resolve_poc_email("") is None
