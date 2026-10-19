"""
The Log shows each ticket as two stages: the category and subcategory the
instructor chose when the request came in, and the ones the ticket got at
On Add (what Zoho was handed back when the ticket was created).
"""
import pytest
from fastapi.testclient import TestClient

from app import classifier, config
from app.main import app
from app.taxonomy import taxonomy
from tests.test_routing_log import (
    GROUP_NAME, LEAF, LEAF_NAME, OTHER_GROUP_NAME, OTHER_LEAF, OTHER_NAME,
    _model_says, _on_add, _post, _presubmit,
)


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


def _stages(client, db, zid):
    t = db.get_ticket_by_zoho_id(zid)
    item = client.get(f"/api/ticket-log/{t['id']}").json()["ticket"]
    return item["came_in"], item["on_add"]


def _fails(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))


def test_instructor_pick_then_the_on_add_category(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, OTHER_LEAF, 0.97)  # overrides the instructor's pick
    pre = _presubmit(cat=GROUP_NAME, sub=LEAF_NAME)
    _on_add("Z-1", pre)
    came_in, on_add = _stages(client, isolated_db, "Z-1")
    assert (came_in["category"], came_in["subcategory"]) == (GROUP_NAME, LEAF_NAME)
    assert (on_add["category"], on_add["subcategory"]) == (OTHER_GROUP_NAME, OTHER_NAME)
    assert came_in["at"] and on_add["at"] and came_in["at"] <= on_add["at"]


def test_same_pick_both_times(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    _on_add("Z-2", _presubmit())
    came_in, on_add = _stages(client, isolated_db, "Z-2")
    assert came_in["subcategory"] == on_add["subcategory"] == LEAF_NAME


def test_no_presubmit_no_pick_and_classify_fails(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    _fails(monkeypatch)
    _post({"zoho_ticket_id": "Z-3", "issue_in_detail": "no pick", "ticket_raised_by": "a@x.in"})
    came_in, on_add = _stages(client, isolated_db, "Z-3")
    assert came_in["category"] is None and came_in["subcategory"] is None
    fb = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    assert (on_add["category"], on_add["subcategory"]) == (fb["parent_name"], fb["name"])


def test_failure_echoes_the_instructor_pick(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    _fails(monkeypatch)
    _post({"zoho_ticket_id": "Z-4", "issue_in_detail": "echo", "ticket_raised_by": "a@x.in",
           "category_of_the_issue": "Zoho Cat", "sub_category_of_the_issue": "Zoho Sub"})
    came_in, on_add = _stages(client, isolated_db, "Z-4")
    assert (came_in["category"], came_in["subcategory"]) == ("Zoho Cat", "Zoho Sub")
    assert (on_add["category"], on_add["subcategory"]) == ("Zoho Cat", "Zoho Sub")


def test_later_transfer_does_not_change_the_on_add_stage(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    pre = _presubmit()
    _on_add("Z-5", pre)
    _post({"zoho_ticket_id": "Z-5", "issue_in_detail": "slides won't load", "ticket_raised_by": "a@x.in",
           "category_of_the_issue": OTHER_GROUP_NAME, "sub_category_of_the_issue": OTHER_NAME})
    t = isolated_db.get_ticket_by_zoho_id("Z-5")
    assert t["category_id"] == OTHER_LEAF  # the transfer happened
    _, on_add = _stages(client, isolated_db, "Z-5")
    assert on_add["subcategory"] == LEAF_NAME


def test_older_ticket_falls_back_to_its_category(client, isolated_db):
    tid = isolated_db.create_ticket("old one", zoho_ticket_id="OLD-1")
    isolated_db.update_ticket(tid, category_id=LEAF)
    came_in, on_add = _stages(client, isolated_db, "OLD-1")
    assert came_in["subcategory"] is None
    assert (on_add["category"], on_add["subcategory"]) == (GROUP_NAME, LEAF_NAME)


def test_unsubmitted_draft_has_no_on_add(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    _presubmit(text="never submitted")
    draft = isolated_db.list_drafts()[0]
    item = client.get(f"/api/ticket-log/{draft['id']}").json()["ticket"]
    assert item["came_in"]["subcategory"] == LEAF_NAME
    assert item["on_add"] is None
