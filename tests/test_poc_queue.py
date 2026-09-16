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


def test_assigned_team_comes_from_taxonomy_leaf(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)  # G01-S01's assigned_team is "IAS/SET"

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["assigned_team"] == "IAS/SET"


def test_issue_summary_is_the_ticket_text_truncated_to_200_chars(isolated_db):
    now = 1_000_000.0
    long_text = "x" * 500
    ticket_id = db.create_ticket(long_text, created_at=now - 3600)
    db.update_ticket(ticket_id, status="classified", category_id="G01-S01")

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["issue_summary"] == "x" * 200


def test_subcategory_heat_includes_zero_count_subcategories_owned_by_poc(isolated_db):
    result = poc_queue.build_subcategory_heat(POC_EMAIL)

    codes = {s["subcategory_code"]: s["open_count"] for s in result["subcategories"]}
    assert codes == {"G01-S01": 0, "G01-S02": 0, "G01-S03": 0}


def test_subcategory_heat_counts_open_tickets_per_subcategory(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, category_id="G01-S01")
    _make_ticket(created_at=now - 3600, category_id="G01-S01")
    _make_ticket(created_at=now - 3600, category_id="G01-S02")

    result = poc_queue.build_subcategory_heat(POC_EMAIL)

    codes = {s["subcategory_code"]: s["open_count"] for s in result["subcategories"]}
    assert codes == {"G01-S01": 2, "G01-S02": 1, "G01-S03": 0}


def test_subcategory_heat_never_includes_a_subcategory_not_owned_by_this_poc(isolated_db):
    result = poc_queue.build_subcategory_heat(POC_EMAIL)

    codes = {s["subcategory_code"] for s in result["subcategories"]}
    assert "G03-S01" not in codes  # routes to catherine/gauthami/ankon, not ranjith
    assert all(code.startswith("G01-") for code in codes)


def test_subcategory_heat_excludes_non_open_status_tickets(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, category_id="G01-S01", status="needs_human_review")

    result = poc_queue.build_subcategory_heat(POC_EMAIL)

    codes = {s["subcategory_code"]: s["open_count"] for s in result["subcategories"]}
    assert codes["G01-S01"] == 0


def test_subcategory_heat_excludes_closed_tickets(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, category_id="G01-S01", raw_payload={"ticket_status": "Resolved By POC"})

    result = poc_queue.build_subcategory_heat(POC_EMAIL)

    codes = {s["subcategory_code"]: s["open_count"] for s in result["subcategories"]}
    assert codes["G01-S01"] == 0


def test_subcategory_heat_ticket_entries_carry_id_and_zoho_ticket_id(isolated_db):
    now = 1_000_000.0
    ticket_id = _make_ticket(created_at=now - 3600, category_id="G01-S01", zoho_ticket_id="Z-9001")

    result = poc_queue.build_subcategory_heat(POC_EMAIL)

    bucket = next(s for s in result["subcategories"] if s["subcategory_code"] == "G01-S01")
    assert bucket["tickets"] == [{"id": ticket_id, "zoho_ticket_id": "Z-9001"}]
    assert bucket["category_code"] == "G01"
    assert bucket["category_name"] == "QA Report / Instructor Evaluation"


def test_ack_resolved_from_real_acknowledgement_history_timestamp(isolated_db):
    now = 1_000_000.0
    history = (
        "--------------------------------\n"
        "Updated By : catherine.joannamathews@nxtwave.co.in\n"
        "Updated On : 12-Aug-2026 04:57 PM\n"
        "Acknowledgement:\n"
        "Hi, can you provide the report link."
    )
    _make_ticket(created_at=now - 3600, raw_payload={"acknowledgement_history": history})

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "acknowledged"
    # 12-Aug-2026 04:57 PM IST -> 11:27 UTC same day
    import datetime as dt
    expected = dt.datetime(2026, 8, 12, 11, 27, tzinfo=dt.timezone.utc).timestamp()
    assert ticket["acknowledged_at"] == pytest.approx(expected)


def test_ack_resolved_uses_earliest_entry_when_history_has_multiple_updates(isolated_db):
    now = 1_000_000.0
    history = (
        "--------------------------------\n"
        "Updated By : someone@nxtwave.co.in\n"
        "Updated On : 12-Aug-2026 04:57 PM\n"
        "Acknowledgement:\nfirst note\n"
        "--------------------------------\n"
        "Updated By : someone@nxtwave.co.in\n"
        "Updated On : 03-Sep-2026 01:13 PM\n"
        "Acknowledgement:\nfollow-up note\n"
    )
    _make_ticket(created_at=now - 3600, raw_payload={"acknowledgement_history": history})

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    import datetime as dt
    expected_first_entry = dt.datetime(2026, 8, 12, 11, 27, tzinfo=dt.timezone.utc).timestamp()
    assert ticket["acknowledged_at"] == pytest.approx(expected_first_entry)


def test_ack_falls_back_to_updated_at_when_ack_text_has_no_parseable_timestamp(isolated_db):
    now = 1_000_000.0
    ticket_id = _make_ticket(
        created_at=now - 3600,
        raw_payload={"acknowledgement_from_the_poc": "Approved and already closed"},
    )
    stored = db.get_ticket(ticket_id)

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "acknowledged"
    assert ticket["acknowledged_at"] == stored["updated_at"]


def test_ack_state_still_missed_with_no_ack_fields_at_all(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - (4 * 3600 + 60))  # 1 minute past the 4h window, no ack fields

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "missed"
    assert ticket["acknowledged_at"] is None


def test_ack_db_column_still_wins_over_acknowledgement_history_if_ever_set(isolated_db):
    now = 1_000_000.0
    history = "Updated On : 12-Aug-2026 04:57 PM\nAcknowledgement:\nnote"
    ticket_id = _make_ticket(
        created_at=now - 3600,
        raw_payload={"acknowledgement_history": history},
    )
    db.update_ticket(ticket_id, acknowledged_at=now - 100)

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["acknowledged_at"] == now - 100
