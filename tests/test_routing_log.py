"""
Routing log: the pre-submit call's draft row, the On Add call claiming it,
drafts staying hidden, duplicate marking, and the Log page's rows and
learning numbers (app/routing_log.py).
"""
import time

from fastapi.testclient import TestClient

from app import classifier, config, routing_log
from app.classifier import ReporterHint, apply_reporter_prior
from app.main import app
from app.models import ClassificationResult
from app.taxonomy import taxonomy

WEBHOOK_SECRET = "test-webhook-secret"
LEAF = "G01-S01"
LEAF_NAME = taxonomy.get(LEAF)["name"]
GROUP = taxonomy.get(LEAF)["parent_id"]
GROUP_NAME = taxonomy.get(LEAF)["parent_name"]
OTHER_LEAF = next(i for i in taxonomy.category_ids if taxonomy.get(i)["parent_id"] != GROUP)
OTHER_NAME = taxonomy.get(OTHER_LEAF)["name"]
OTHER_GROUP_NAME = taxonomy.get(OTHER_LEAF)["parent_name"]


def _result(category_id, confidence, needs_clarification=False):
    return ClassificationResult(
        category_id=category_id, confidence=confidence, reasoning="model says so",
        needs_clarification=needs_clarification,
        clarifying_question="Which one?" if needs_clarification else None,
    )


def _model_says(monkeypatch, category_id, confidence):
    """A fake classify() that still runs the real reporter-pick backstop."""
    calls = []

    def fake(text, api_key, embed_api_key=None, reporter_hint=None):
        calls.append(text)
        return apply_reporter_prior(_result(category_id, confidence), reporter_hint)

    monkeypatch.setattr(config, "ZOHO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(classifier, "classify", fake)
    return calls


def _post(payload):
    return TestClient(app).post(
        "/api/webhooks/zoho/tickets", json=payload, headers={"X-Webhook-Secret": WEBHOOK_SECRET}
    )


def _presubmit(text="slides won't load", sub=LEAF_NAME, cat=GROUP_NAME, who="a@x.in"):
    return _post({"issue_in_detail": text, "category_of_the_issue": cat,
                  "sub_category_of_the_issue": sub, "ticket_raised_by": who}).json()


def _on_add(zid, pre, text="slides won't load", who="a@x.in"):
    return _post({"zoho_ticket_id": zid, "issue_in_detail": text, "ticket_raised_by": who,
                  "category_of_the_issue": pre["category_of_the_issue"],
                  "sub_category_of_the_issue": pre["sub_category_of_the_issue"]}).json()


def _drafts(db):
    with db._conn() as conn:
        return db._fetchall(conn, "SELECT * FROM tickets WHERE status = 'draft'")


# --- apply_reporter_prior records the decision ----------------------------------

def test_decision_fields_for_each_outcome():
    hint = ReporterHint(GROUP, LEAF)
    agreed = apply_reporter_prior(_result(LEAF, 0.4), hint)
    kept = apply_reporter_prior(_result(OTHER_LEAF, 0.7), hint)
    overrode = apply_reporter_prior(_result(OTHER_LEAF, 0.99), hint)
    assert (agreed.decision, agreed.model_category_id, agreed.model_confidence) == ("agreed", LEAF, 0.4)
    assert (kept.decision, kept.category_id, kept.model_category_id, kept.model_confidence) == ("kept", LEAF, OTHER_LEAF, 0.7)
    assert (overrode.decision, overrode.category_id, overrode.model_confidence) == ("overrode", OTHER_LEAF, 0.99)
    assert apply_reporter_prior(_result(OTHER_LEAF, 0.5), ReporterHint(GROUP)).decision is None


# --- step 1: pre-submit stores a draft ------------------------------------------

def test_presubmit_stores_a_hidden_draft_with_both_picks(isolated_db, monkeypatch):
    _model_says(monkeypatch, OTHER_LEAF, 0.7)
    pre = _presubmit()
    assert pre["sub_category_of_the_issue"] == LEAF_NAME  # kept the instructor's pick

    [d] = _drafts(isolated_db)
    assert d["zoho_ticket_id"] is None
    assert (d["reporter_category"], d["reporter_subcategory"], d["reporter_leaf_id"]) == (GROUP_NAME, LEAF_NAME, LEAF)
    assert (d["model_category_id"], d["model_confidence"], d["decision"]) == (OTHER_LEAF, 0.7, "kept")
    assert d["category_id"] == LEAF
    assert isolated_db.list_all_tickets() == []


def test_presubmit_failure_still_stores_a_draft(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    monkeypatch.setattr(classifier, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    body = _presubmit()
    assert body["sub_category_of_the_issue"] == LEAF_NAME
    [d] = _drafts(isolated_db)
    assert d["category_id"] is None and "failed" in d["reasoning"]


# --- step 2: On Add claims the draft --------------------------------------------

def test_on_add_claims_the_draft_instead_of_a_new_row(isolated_db, monkeypatch):
    calls = _model_says(monkeypatch, OTHER_LEAF, 0.99)
    pre = _presubmit()
    _on_add("Z-1", pre)

    assert len(calls) == 1
    assert _drafts(isolated_db) == []
    [t] = isolated_db.list_all_tickets()
    assert t["zoho_ticket_id"] == "Z-1" and t["status"] == "classified"
    assert (t["decision"], t["reporter_leaf_id"], t["model_category_id"]) == ("overrode", LEAF, OTHER_LEAF)
    assert t["category_id"] == OTHER_LEAF


def test_on_add_takes_the_oldest_matching_draft(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    pre = _presubmit()
    first = _drafts(isolated_db)[0]["id"]
    _presubmit()
    _on_add("Z-2", pre)
    assert isolated_db.get_ticket_by_zoho_id("Z-2")["id"] == first
    assert len(_drafts(isolated_db)) == 1  # the other stays a hidden draft


def test_on_add_ignores_drafts_older_than_two_hours(isolated_db, monkeypatch):
    calls = _model_says(monkeypatch, LEAF, 0.9)
    pre = _presubmit()
    old = _drafts(isolated_db)[0]["id"]
    with isolated_db._conn() as conn:
        isolated_db._exec(conn, "UPDATE tickets SET created_at = ? WHERE id = ?", (time.time() - 3 * 3600, old))
    _on_add("Z-3", pre)

    assert isolated_db.get_ticket_by_zoho_id("Z-3")["id"] != old
    assert [d["id"] for d in _drafts(isolated_db)] == [old]
    assert len(calls) == 1  # adopted the form's category, no second model call


def test_on_add_reclassifies_when_presubmit_failed_using_the_instructors_pick(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    monkeypatch.setattr(classifier, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    pre = _presubmit()
    seen = {}

    def fake(text, api_key, embed_api_key=None, reporter_hint=None):
        seen["hint"] = reporter_hint
        return apply_reporter_prior(_result(LEAF, 0.9), reporter_hint)

    monkeypatch.setattr(classifier, "classify", fake)
    _on_add("Z-4", pre)
    t = isolated_db.get_ticket_by_zoho_id("Z-4")
    assert seen["hint"] == ReporterHint(GROUP, LEAF)
    assert (t["status"], t["decision"]) == ("classified", "agreed")


# --- step 3: drafts stay hidden ---------------------------------------------------

def test_drafts_are_hidden_everywhere(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    _presubmit()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    assert isolated_db.list_all_tickets() == []
    assert isolated_db.list_tickets_for_date(today) == []
    assert isolated_db.list_pending_tickets() == []          # the 30-minute job's queue
    assert isolated_db.count_pending_tickets() == 0
    assert isolated_db.list_tickets_since(0) == []
    assert isolated_db.count_drafts() == 1


def test_background_job_never_classifies_drafts(isolated_db, monkeypatch):
    calls = _model_says(monkeypatch, LEAF, 0.9)
    _presubmit()
    from app.main import _classify_pending_batch
    out = _classify_pending_batch(10, "sk-or-test")
    assert out["classified"] == 0 and len(calls) == 1  # only the pre-submit call itself


# --- step 4: duplicates -----------------------------------------------------------

def test_same_text_same_instructor_within_7_days_is_a_duplicate(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    _on_add("Z-10", _presubmit(text="Mic not working"), text="Mic not working")
    _on_add("Z-11", _presubmit(text="mic  NOT working"), text="mic  NOT working")
    _on_add("Z-12", _presubmit(text="Mic not working", who="b@x.in"), text="Mic not working", who="b@x.in")
    first = isolated_db.get_ticket_by_zoho_id("Z-10")
    assert first["duplicate_of"] is None
    assert isolated_db.get_ticket_by_zoho_id("Z-11")["duplicate_of"] == first["id"]
    assert isolated_db.get_ticket_by_zoho_id("Z-12")["duplicate_of"] is None  # different instructor


def test_older_than_7_days_is_not_a_duplicate(isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    _on_add("Z-20", _presubmit(text="Mic not working"), text="Mic not working")
    first = isolated_db.get_ticket_by_zoho_id("Z-20")
    isolated_db.update_ticket(first["id"], created_at=time.time() - 8 * 86400)
    _on_add("Z-21", _presubmit(text="Mic not working"), text="Mic not working")
    assert isolated_db.get_ticket_by_zoho_id("Z-21")["duplicate_of"] is None


# --- step 5/6: log rows and learning numbers ---------------------------------------

def _row(decision, model, conf, final, outcome="right", reporter=LEAF, dup=None):
    return {"decision": decision, "model_category_id": model, "model_confidence": conf,
            "reporter_leaf_id": reporter, "final_category_id": final, "outcome": outcome, "duplicate_of": dup}


def test_build_rows_right_wrong_and_pending():
    tickets = [
        {"id": "a", "status": "classified", "category_id": LEAF, "created_at": 1},
        {"id": "b", "status": "corrected", "category_id": OTHER_LEAF, "created_at": 2},
        {"id": "c", "status": "needs_human_review", "category_id": LEAF, "created_at": 3},
    ]
    corrections = [{"ticket_id": "b", "predicted_category_id": LEAF, "corrected_category_id": OTHER_LEAF,
                    "corrected_by": "zoho-transfer"}]
    rows = {r["id"]: r for r in routing_log.build_rows(tickets, corrections)}
    assert rows["a"]["outcome"] == "right"
    assert (rows["b"]["outcome"], rows["b"]["routed_category_id"], rows["b"]["final_category_id"]) == ("wrong", LEAF, OTHER_LEAF)
    assert rows["b"]["corrected_by"] == "zoho-transfer"
    assert rows["c"]["outcome"] == "pending"


def test_learn_per_decision_and_best_threshold():
    rows = [
        _row("overrode", OTHER_LEAF, 0.97, OTHER_LEAF),               # model right
        _row("overrode", OTHER_LEAF, 0.85, LEAF, "wrong"),            # instructor right
        _row("kept", OTHER_LEAF, 0.75, LEAF),                         # instructor right
        _row("kept", OTHER_LEAF, 0.7, OTHER_LEAF, "wrong"),           # model would have been right
        _row("agreed", LEAF, 0.9, LEAF),
        _row("agreed", LEAF, 0.9, LEAF, dup="x"),                     # duplicate - not counted
        _row("kept", OTHER_LEAF, 0.6, LEAF, "pending"),               # not settled - not counted
    ]
    s = routing_log.learn(rows, 0.8)
    assert s["settled"] == 5 and s["duplicates"] == 1
    assert s["by_decision"]["overrode"]["model_right"] == 1 and s["by_decision"]["overrode"]["instructor_right"] == 1
    assert s["by_decision"]["kept"]["instructor_right_pct"] == 50.0
    assert s["by_decision"]["agreed"]["n"] == 1
    # 4 disagreements. Override when confidence >= t, else keep the instructor's pick:
    #   t <= 0.70: all overridden          -> 0.97 right, 0.85 wrong, 0.75 wrong, 0.70 right = 2
    #   t = 0.75:  0.70 kept               -> 1
    #   t = 0.80, 0.85: 0.75/0.70 kept     -> 2
    #   t >= 0.90: only 0.97 overridden    -> 3
    sim = {x["threshold"]: x["right"] for x in s["simulation"]}
    assert (sim[0.7], sim[0.75], sim[0.8], sim[0.9], sim[0.95]) == (2, 1, 2, 3, 3)
    assert s["disagreements"] == 4
    assert s["best_threshold"] == 0.9  # tied with 0.95; 0.9 is nearer the current 0.8


def test_no_subcategory_pick_records_decision_none_and_the_models_pick(isolated_db, monkeypatch):
    _model_says(monkeypatch, OTHER_LEAF, 0.6)
    pre = _presubmit(sub=None)
    _on_add("Z-40", pre)
    t = isolated_db.get_ticket_by_zoho_id("Z-40")
    assert (t["decision"], t["model_category_id"], t["model_confidence"]) == ("none", OTHER_LEAF, 0.6)
    assert (t["reporter_category"], t["reporter_subcategory"], t["reporter_leaf_id"]) == (GROUP_NAME, None, None)


def test_routing_log_endpoint(isolated_db, monkeypatch):
    _model_says(monkeypatch, OTHER_LEAF, 0.99)
    _on_add("Z-30", _presubmit())
    _presubmit(text="abandoned")
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    assert TestClient(app).get("/api/routing-log").status_code == 401
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    body = c.get("/api/routing-log").json()
    [row] = body["rows"]
    assert (row["zoho_ticket_id"], row["decision"], row["reporter_subcategory"]) == ("Z-30", "overrode", LEAF_NAME)
    assert row["final_category_name"] == OTHER_NAME and row["outcome"] == "right"
    assert body["drafts"] == 1
    assert body["summary"]["by_decision"]["overrode"]["n"] == 1
