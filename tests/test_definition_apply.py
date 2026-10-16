"""
Classify page: apply a definition change and undo it.
"""
import pytest

from app import db, main
from app.models import ClassificationResult
from app.taxonomy import taxonomy
from tests.test_classify_page import PW, _leaf, client  # noqa: F401

RIGHT_LINE = "Also route here when ZZZ."
WRONG_LINE = "NOT this: ZZZ -> A > B."


@pytest.fixture()
def fake_recheck(monkeypatch):
    def fake(text, api_key, **kw):
        return ClassificationResult(category_id=_leaf(1)["id"], confidence=0.9, reasoning="r",
                                    needs_clarification=False)

    monkeypatch.setattr(main.classifier, "classify", fake)


def _body():
    return {"text": "an issue", "wrong_category_id": _leaf(0)["id"], "right_category_id": _leaf(1)["id"],
            "right_line": RIGHT_LINE, "wrong_line": WRONG_LINE}


def _desc(i):
    return taxonomy.get(_leaf(i)["id"]).get("description", "")


def test_apply_saves_and_records(client, fake_recheck):
    b0, b1 = _desc(0), _desc(1)
    version = taxonomy.version
    res = client.post("/api/classify/apply", json=_body(), headers=PW)
    assert res.status_code == 200
    body = res.json()
    assert body["recheck"]["correct"] is True
    assert RIGHT_LINE in _desc(1) and WRONG_LINE in _desc(0)
    assert taxonomy.version == version + 1
    row = db.get_taxonomy_change(body["change_id"])
    assert row["created_by"] == "admin" and row["undone_at"] is None
    assert row["changes"][_leaf(1)["id"]][0] == b1
    assert row["changes"][_leaf(0)["id"]][0] == b0


def test_apply_twice_is_noop(client, fake_recheck):
    client.post("/api/classify/apply", json=_body(), headers=PW)
    version = taxonomy.version
    res = client.post("/api/classify/apply", json=_body(), headers=PW)
    assert res.status_code == 200 and res.json()["change_id"] is None
    assert taxonomy.version == version


def test_apply_recheck_failure_still_ok(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(main.classifier, "classify", boom)
    res = client.post("/api/classify/apply", json=_body(), headers=PW)
    assert res.status_code == 200
    assert res.json()["recheck"] is None and "down" in res.json()["recheck_error"]
    assert RIGHT_LINE in _desc(1)


def test_undo_restores(client, fake_recheck):
    b0, b1 = _desc(0), _desc(1)
    cid = client.post("/api/classify/apply", json=_body(), headers=PW).json()["change_id"]
    res = client.post("/api/classify/undo", json={"change_id": cid}, headers=PW)
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert _desc(0) == b0 and _desc(1) == b1
    assert db.get_taxonomy_change(cid)["undone_at"] is not None


def test_undo_refuses_when_changed_since(client, fake_recheck):
    cid = client.post("/api/classify/apply", json=_body(), headers=PW).json()["change_id"]
    data = taxonomy.as_dict()
    for g in data["categories"]:
        for s in g.get("subcategories", []):
            if s["id"] == _leaf(1)["id"]:
                s["description"] = "Newer text."
    taxonomy.save(data)
    res = client.post("/api/classify/undo", json={"change_id": cid}, headers=PW)
    assert res.status_code == 409
    assert "changed again" in res.json()["detail"]
    assert _desc(1) == "Newer text."
    assert WRONG_LINE in _desc(0)
    assert db.get_taxonomy_change(cid)["undone_at"] is None


def test_undo_twice_conflicts(client, fake_recheck):
    cid = client.post("/api/classify/apply", json=_body(), headers=PW).json()["change_id"]
    assert client.post("/api/classify/undo", json={"change_id": cid}, headers=PW).status_code == 200
    assert client.post("/api/classify/undo", json={"change_id": cid}, headers=PW).status_code == 409
    assert client.post("/api/classify/undo", json={"change_id": "nope"}, headers=PW).status_code == 404


def test_apply_requires_password(client, fake_recheck):
    before = _desc(1)
    assert client.post("/api/classify/apply", json=_body()).status_code == 403
    assert client.post("/api/classify/undo", json={"change_id": "x"}).status_code == 403
    assert _desc(1) == before
