"""Timeline events recorded by the Zoho webhook and the auto-classify loop."""
from app import classifier, config, db as app_db, ticket_events
from app.main import _classify_pending_batch
from tests.test_routing_log import (
    GROUP_NAME, LEAF, LEAF_NAME, OTHER_LEAF, _model_says, _on_add, _post, _presubmit,
)


def _kinds(db, zid):
    t = db.get_ticket_by_zoho_id(zid)
    return t["id"], [e["kind"] for e in db.list_events(t["id"])]


def test_presubmit_then_on_add_builds_one_timeline(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    pre = _presubmit()
    _on_add("Z-1", pre)
    _, kinds = _kinds(isolated_db, "Z-1")
    assert kinds == ["presubmit", "on_add"]


def test_classify_failure_records_error(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    monkeypatch.setattr(classifier, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pre = _presubmit()
    _on_add("Z-2", pre)
    tid, kinds = _kinds(isolated_db, "Z-2")
    assert "error" in kinds
    assert any("boom" in (e["error"] or "") for e in isolated_db.list_events(tid))


def test_event_failure_never_breaks_webhook(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(isolated_db, "add_event", boom)
    pre = _presubmit()
    assert pre["sub_category_of_the_issue"] == LEAF_NAME
    body = _on_add("Z-3", pre)
    assert body["sub_category_of_the_issue"] == LEAF_NAME


def test_line_builder_failure_never_breaks_webhook(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)

    def boom(*a, **k):
        raise RuntimeError("bad line")

    for name in ("presubmit_line", "on_add_line", "edit_line", "transfer_line", "duplicate_line", "auto_classify_line"):
        monkeypatch.setattr(ticket_events, name, boom)
    pre = _presubmit()
    assert pre["sub_category_of_the_issue"] == LEAF_NAME
    assert _on_add("Z-4", pre)["sub_category_of_the_issue"] == LEAF_NAME


def test_transfer_and_edit_recorded(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    pre = _presubmit()
    _on_add("Z-5", pre)
    other = ticket_events.taxonomy.get(OTHER_LEAF)
    _post({"zoho_ticket_id": "Z-5", "issue_in_detail": "slides won't load", "ticket_status": "Closed",
           "category_of_the_issue": pre["category_of_the_issue"],
           "sub_category_of_the_issue": pre["sub_category_of_the_issue"]})
    _, kinds = _kinds(isolated_db, "Z-5")
    assert kinds.count("edit") == 1 and "transfer" not in kinds
    _post({"zoho_ticket_id": "Z-5", "issue_in_detail": "slides won't load", "ticket_status": "Closed",
           "category_of_the_issue": other["parent_name"], "sub_category_of_the_issue": other["name"]})
    _, kinds = _kinds(isolated_db, "Z-5")
    assert kinds.count("edit") == 1 and "transfer" in kinds


def test_auto_classify_records_event(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    tid = isolated_db.create_ticket("old ticket text", zoho_ticket_id="Z-6", raw_payload={})
    assert _classify_pending_batch(10, "k")["classified"] == 1
    assert [e["kind"] for e in isolated_db.list_events(tid)] == ["auto_classify"]
