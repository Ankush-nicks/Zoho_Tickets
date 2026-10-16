"""
Classify page: draft a definition change and preview its effect (saves nothing).
"""
import copy
import json
from types import SimpleNamespace

from app import classifier, config, definition_drafter, main
from app.models import ClassificationResult
from app.taxonomy import Taxonomy, taxonomy
from tests.test_classify_page import PW, _leaf, client  # noqa: F401

ISSUE = "a brand new issue"


def _subs():
    return [s for g in taxonomy.groups for s in g.get("subcategories", [])]


def _two_with_examples():
    return [s for s in _subs() if s.get("examples")][:2]


def _fake_openai(monkeypatch, module, reply):
    class Completions:
        def create(self, **kw):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])

    class Fake:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(module, "OpenAI", Fake)


def _result(cid):
    return ClassificationResult(category_id=cid, confidence=0.9, reasoning="r", needs_clarification=False)


def test_draft_lines_are_normalised(monkeypatch):
    wrong, right = _subs()[0], _subs()[1]
    _fake_openai(monkeypatch, definition_drafter,
                 {"right_line": "the user cannot log in", "wrong_line": "password resets"})
    out = definition_drafter.draft_lines("cannot log in", wrong["id"], right["id"], "k")
    assert out["right_line"].startswith("Also route here when")
    assert out["wrong_line"].startswith("NOT this: ")
    parent = taxonomy.get(right["id"])["parent_name"]
    assert out["wrong_line"].endswith(f" -> {parent} > {right['name']}.")


def test_draft_rejects_same_category(client):
    a = _leaf(0)["id"]
    body = {"text": "x", "wrong_category_id": a, "right_category_id": a}
    assert client.post("/api/classify/draft", json=body, headers=PW).status_code == 400
    body = {"text": "x", "wrong_category_id": a, "right_category_id": "nope"}
    assert client.post("/api/classify/draft", json=body, headers=PW).status_code == 400
    body = {"text": "x", "wrong_category_id": a, "right_category_id": _leaf(1)["id"]}
    assert client.post("/api/classify/draft", json=body).status_code == 403


def test_draft_endpoint_and_failure(client, monkeypatch):
    body = {"text": "x", "wrong_category_id": _leaf(0)["id"], "right_category_id": _leaf(1)["id"]}
    monkeypatch.setattr(main.definition_drafter, "draft_lines",
                        lambda *a, **k: {"right_line": "R", "wrong_line": "W"})
    assert client.post("/api/classify/draft", json=body, headers=PW).json() == {"right_line": "R", "wrong_line": "W"}

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(main.definition_drafter, "draft_lines", boom)
    assert client.post("/api/classify/draft", json=body, headers=PW).status_code == 502
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert client.post("/api/classify/draft", json=body, headers=PW).status_code == 503


def test_apply_lines_skips_existing():
    data = taxonomy.as_dict()
    right, wrong = _subs()[1], _subs()[0]
    args = (data, right["id"], wrong["id"], "Also route here when A.", "NOT this: B -> X > Y.")
    changes = definition_drafter.apply_lines(*args)
    assert set(changes) == {right["id"], wrong["id"]}
    before, after = changes[right["id"]]
    assert after == (before + " Also route here when A.").strip()
    again = definition_drafter.apply_lines(*args)
    assert again[right["id"]][0] == again[right["id"]][1]
    assert again[wrong["id"]][0] == again[wrong["id"]][1]


def _preview_body(right, wrong, text=ISSUE):
    return {"text": text, "wrong_category_id": wrong["id"], "right_category_id": right["id"],
            "right_line": "Also route here when ZZZ.", "wrong_line": "NOT this: ZZZ -> A > B."}


def test_preview_marks_got_worse(client, monkeypatch):
    right, wrong = _two_with_examples()
    victim = right["examples"][0]
    calls = []

    def fake(text, api_key, embed_api_key=None, reporter_hint=None, tax=None, exclude_text=None):
        calls.append((text, tax is not None, exclude_text))
        if text == ISSUE:
            return _result(right["id"])
        owner = next(s for s in _subs() if text in s.get("examples", []))
        return _result(wrong["id"] if (tax is not None and text == victim) else owner["id"])

    monkeypatch.setattr(main.classifier, "classify", fake)
    res = client.post("/api/classify/preview", json=_preview_body(right, wrong), headers=PW)
    assert res.status_code == 200
    cases = res.json()["cases"]
    assert cases[0]["text"] == ISSUE and cases[0]["expected"]["category_id"] == right["id"]
    worse = [c for c in cases if c["got_worse"]]
    assert [c["text"] for c in worse] == [victim]
    assert worse[0]["before_ok"] and not worse[0]["after_ok"]
    assert all(c["text"] != ISSUE for c in cases[1:])
    # every row, the issue included, is checked without its own example
    assert (ISSUE, False, ISSUE) in calls and (ISSUE, True, ISSUE) in calls
    assert (victim, False, victim) in calls and (victim, True, victim) in calls


def test_preview_case_error_is_isolated(client, monkeypatch):
    right, wrong = _two_with_examples()
    bad = right["examples"][0]

    def fake(text, api_key, embed_api_key=None, reporter_hint=None, tax=None, exclude_text=None):
        if text == bad:
            raise RuntimeError("model down")
        return _result(right["id"])

    monkeypatch.setattr(main.classifier, "classify", fake)
    res = client.post("/api/classify/preview", json=_preview_body(right, wrong), headers=PW)
    assert res.status_code == 200
    case = next(c for c in res.json()["cases"] if c["text"] == bad)
    assert case["before"] is None and case["after"] is None and "model down" in case["error"]
    assert case["got_worse"] is False


def test_preview_saves_nothing(client, monkeypatch):
    right, wrong = _two_with_examples()
    snap = copy.deepcopy(taxonomy.as_dict())
    version = taxonomy.version
    monkeypatch.setattr(
        main.classifier, "classify",
        lambda text, api_key, embed_api_key=None, reporter_hint=None, tax=None, exclude_text=None: _result(right["id"]))
    assert client.post("/api/classify/preview", json=_preview_body(right, wrong), headers=PW).status_code == 200
    assert taxonomy.as_dict() == snap
    assert taxonomy.version == version


def test_classify_uses_given_taxonomy(monkeypatch):
    data = taxonomy.as_dict()
    data["categories"][0]["subcategories"][0]["description"] = "ONLY-IN-PASSED-TAX-XYZ"
    tax = Taxonomy.from_data(data)
    seen = {}

    class Completions:
        def create(self, **kw):
            seen.update(kw)
            raw = {"category_id": tax.category_ids[0], "confidence": 0.9, "reasoning": "r",
                   "needs_clarification": False, "clarifying_question": None}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(raw)))])

    class Fake:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(classifier, "OpenAI", Fake)
    monkeypatch.setattr(classifier, "_retrieve_fewshot", lambda *a, **k: [])
    classifier.classify("hello", "k", tax=tax)
    assert "ONLY-IN-PASSED-TAX-XYZ" in seen["messages"][0]["content"]
    assert "ONLY-IN-PASSED-TAX-XYZ" not in taxonomy.as_prompt_block()


def test_retrieve_fewshot_excludes_text(monkeypatch):
    monkeypatch.setattr(classifier.memory, "seed_if_empty", lambda *a, **k: None)
    monkeypatch.setattr(classifier.memory, "retrieve_similar", lambda *a, **k: [
        {"text": "Same  Thing", "category_id": "a"}, {"text": "other", "category_id": "b"}])
    out = classifier._retrieve_fewshot("q", "k", exclude_text="same thing")
    assert [e["text"] for e in out] == ["other"]
