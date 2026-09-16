from app import db


def test_acknowledged_at_round_trips(isolated_db):
    ticket_id = db.create_ticket("Some issue text", created_at=1000.0)

    db.update_ticket(ticket_id, acknowledged_at=1234.5)

    ticket = db.get_ticket(ticket_id)
    assert ticket["acknowledged_at"] == 1234.5


def test_acknowledged_at_defaults_to_none(isolated_db):
    ticket_id = db.create_ticket("Some other issue text", created_at=1000.0)

    ticket = db.get_ticket(ticket_id)
    assert ticket["acknowledged_at"] is None
