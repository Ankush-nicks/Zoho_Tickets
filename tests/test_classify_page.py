"""
Classify page: suggest a category for pasted text (writes nothing), and
learn from a password-protected correction.
"""
import hashlib

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
