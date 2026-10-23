"""
Classify page: suggest a category for pasted text (writes nothing), and
learn from a password-protected correction.
"""
import hashlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, main, memory
from app.main import app
from app.models import ClassificationResult
from app.taxonomy import taxonomy

PW = {"X-Taxonomy-Password": "pw"}


def _leaf(i=0):
    return [s for g in taxonomy.groups for s in g.get("subcategories", [])][i]


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    monkeypatch.setattr(config, "TAXONOMY_EDIT_PASSWORD", "pw")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "dummy")
    monkeypatch.setattr(
        memory, "_embed",
        lambda texts, api_key: [[(b + 1) / 256 for b in hashlib.md5(t.encode()).digest()] for t in texts],
    )
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


@pytest.fixture()
def fake_classify(monkeypatch):
    state = {"leaf": _leaf(0)["id"], "texts": []}

    def fake(text, api_key, embed_api_key=None, reporter_hint=None):
        state["texts"].append(text)
        return ClassificationResult(category_id=state["leaf"], confidence=0.9, reasoning="because",
                                    needs_clarification=False)

    monkeypatch.setattr(main.classifier, "classify", fake)
    return state


def test_suggest_returns_labels(client, fake_classify):
    leaf = _leaf(0)
    res = client.post("/api/classify", json={"text": "my laptop is broken"})
    assert res.status_code == 200
    body = res.json()
    assert body["category_id"] == leaf["id"]
    assert body["subcategory"] == leaf["name"]
    assert body["category"] == taxonomy.get(leaf["id"])["parent_name"]
    assert body["confidence"] == 0.9 and body["reasoning"] == "because"


def test_suggest_never_creates_ticket(client, fake_classify):
    client.post("/api/classify", json={"text": "something"})
    assert db.list_all_tickets() == []
    assert db.count_drafts() == 0


def test_suggest_empty_text_and_missing_key(client, fake_classify, monkeypatch):
    assert client.post("/api/classify", json={"text": "   "}).status_code == 400
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert client.post("/api/classify", json={"text": "x"}).status_code == 503


def test_correct_adds_example_and_rechecks(client, fake_classify):
    leaf = _leaf(1)
    fake_classify["leaf"] = leaf["id"]
    before = len(taxonomy.get(leaf["id"]).get("examples", []))
    res = client.post("/api/classify/correct", json={"text": "  Brand   new  issue ", "category_id": leaf["id"]}, headers=PW)
    assert res.status_code == 200
    body = res.json()
    assert body["added"] is True
    assert body["recheck"]["correct"] is True and body["recheck"]["category_id"] == leaf["id"]
    examples = taxonomy.get(leaf["id"])["examples"]
    assert len(examples) == before + 1 and examples[-1] == "Brand new issue"
    assert fake_classify["texts"][-1] == "Brand   new  issue"

    fake_classify["leaf"] = _leaf(0)["id"]
    res = client.post("/api/classify/correct", json={"text": "another one", "category_id": leaf["id"]}, headers=PW)
    assert res.json()["recheck"]["correct"] is False


def test_correct_dedupes_example(client, fake_classify):
    leaf = _leaf(1)
    client.post("/api/classify/correct", json={"text": "Same Text here", "category_id": leaf["id"]}, headers=PW)
    n = len(taxonomy.get(leaf["id"])["examples"])
    res = client.post("/api/classify/correct", json={"text": "same   text  HERE", "category_id": leaf["id"]}, headers=PW)
    assert res.status_code == 200 and res.json()["added"] is False
    assert len(taxonomy.get(leaf["id"])["examples"]) == n


def test_correct_requires_password(client, fake_classify):
    leaf = _leaf(1)
    body = {"text": "x y", "category_id": leaf["id"]}
    assert client.post("/api/classify/correct", json=body).status_code == 403
    assert client.post("/api/classify/correct", json=body, headers={"X-Taxonomy-Password": "no"}).status_code == 403


def test_correct_rejects_unknown_category(client, fake_classify):
    res = client.post("/api/classify/correct", json={"text": "x", "category_id": "nope"}, headers=PW)
    assert res.status_code == 400


def test_classify_long_text(client, fake_classify):
    assert client.post("/api/classify", json={"text": "a" * 45000}).status_code == 200


def test_correct_syncs_memory(client, fake_classify):
    leaf = _leaf(1)
    res = client.post("/api/classify/correct", json={"text": "Memory sync probe text", "category_id": leaf["id"]}, headers=PW)
    assert res.json()["memory_synced"] is True
    assert memory.seeds_match(taxonomy.seed_examples())
    assert "Memory sync probe text" in memory._collection.get(where={"source": "seed"})["documents"]


def test_correct_reports_memory_sync_failure(client, fake_classify, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    leaf = _leaf(1)
    res = client.post("/api/classify/correct", json={"text": "no memory today", "category_id": leaf["id"]}, headers=PW)
    assert res.status_code == 200
    assert res.json()["added"] is True and res.json()["memory_synced"] is False
    assert "no memory today" in taxonomy.get(leaf["id"])["examples"]


def test_correct_recheck_failure_is_not_an_error(client, fake_classify, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x" * 500)

    monkeypatch.setattr(main.classifier, "classify", boom)
    leaf = _leaf(1)
    res = client.post("/api/classify/correct", json={"text": "recheck will fail", "category_id": leaf["id"]}, headers=PW)
    assert res.status_code == 200
    body = res.json()
    assert body["added"] is True and body["recheck"] is None and len(body["recheck_error"]) == 300
    assert "recheck will fail" in taxonomy.get(leaf["id"])["examples"]


def test_concurrent_corrections_both_kept(client, fake_classify, monkeypatch):
    leaf = _leaf(1)
    real = taxonomy.as_dict

    def slow():
        d = real()
        time.sleep(0.2)
        return d

    monkeypatch.setattr(taxonomy, "as_dict", slow)
    results = []

    def go(text):
        results.append(client.post("/api/classify/correct", json={"text": text, "category_id": leaf["id"]}, headers=PW).status_code)

    threads = [threading.Thread(target=go, args=(t,)) for t in ("first concurrent", "second concurrent")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [200, 200]
    examples = taxonomy.get(leaf["id"])["examples"]
    assert "first concurrent" in examples and "second concurrent" in examples


def test_stale_taxonomy_put_is_rejected(client, fake_classify):
    stale = taxonomy.as_dict()
    taxonomy.save(taxonomy.as_dict())  # someone else saved: version moves on
    live = taxonomy.as_dict()
    stale["categories"][0]["subcategories"][0]["description"] = "stale edit"
    res = client.put("/api/taxonomy", json=stale, headers=PW)
    assert res.status_code == 409
    assert "reload the Taxonomy page" in res.json()["detail"]
    assert taxonomy.as_dict() == live


def test_current_version_taxonomy_put_still_works(client, fake_classify):
    data = taxonomy.as_dict()
    v = data["version"]
    data["categories"][0]["subcategories"][0]["description"] = "fresh edit"
    res = client.put("/api/taxonomy", json=data, headers=PW)
    assert res.status_code == 200 and res.json()["version"] == (v or 0) + 1


def test_correction_then_put_with_old_version_keeps_the_example(client, fake_classify):
    leaf = _leaf(1)
    pre = taxonomy.as_dict()
    client.post("/api/classify/correct", json={"text": "learned only once", "category_id": leaf["id"]}, headers=PW)
    res = client.put("/api/taxonomy", json=pre, headers=PW)
    assert res.status_code == 409
    assert "learned only once" in taxonomy.get(leaf["id"])["examples"]
    stored = {sub["id"]: sub for g in db.get_taxonomy_doc()["categories"] for sub in g.get("subcategories", [])}
    assert "learned only once" in stored[leaf["id"]]["examples"]


# The Classify page must run the same process as a live ticket: the model's
# pick weighed against the instructor's Zoho pick (apply_reporter_prior).
def _routable_leaves():
    fallback_group = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)["parent_id"]
    return [s for g in taxonomy.groups if g["id"] != fallback_group for s in g.get("subcategories", [])]


@pytest.fixture()
def prior_classify(monkeypatch):
    real_prior = main.classifier.apply_reporter_prior
    model_leaf, instructor_leaf = _routable_leaves()[:2]
    state = {"leaf": model_leaf["id"], "confidence": 0.7, "hints": []}

    def fake(text, api_key, embed_api_key=None, reporter_hint=None):
        state["hints"].append(reporter_hint)
        raw = ClassificationResult(category_id=state["leaf"], confidence=state["confidence"],
                                   reasoning="because", needs_clarification=False)
        return real_prior(raw, reporter_hint)

    monkeypatch.setattr(main.classifier, "classify", fake)
    return state, model_leaf, instructor_leaf


def test_suggest_keeps_instructor_pick_like_the_log(client, prior_classify):
    state, model_leaf, instructor_leaf = prior_classify
    res = client.post("/api/classify", json={
        "text": "something",
        "instructor_category": taxonomy.get(instructor_leaf["id"])["parent_name"],
        "instructor_subcategory": instructor_leaf["name"],
    })
    assert res.status_code == 200
    body = res.json()
    assert state["hints"][-1].leaf_id == instructor_leaf["id"]
    assert body["category_id"] == instructor_leaf["id"]
    assert body["decision"] == "kept"
    assert body["model_category_id"] == model_leaf["id"] and body["model_confidence"] == 0.7
    assert "instructor's pick was kept" in body["decision_text"]


def test_suggest_confident_model_overrides_instructor(client, prior_classify):
    state, model_leaf, instructor_leaf = prior_classify
    state["confidence"] = 0.95
    body = client.post("/api/classify", json={
        "text": "something", "instructor_subcategory": instructor_leaf["name"],
    }).json()
    assert body["category_id"] == model_leaf["id"] and body["decision"] == "overrode"


def test_suggest_without_instructor_pick_is_model_only(client, prior_classify):
    state, model_leaf, _ = prior_classify
    body = client.post("/api/classify", json={"text": "something"}).json()
    assert state["hints"][-1] is None
    assert body["category_id"] == model_leaf["id"] and body["decision"] == "none"
