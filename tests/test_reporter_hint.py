"""
The instructor's own Zoho category/subcategory pick as a classification
signal: resolving it to a ReporterHint, the prompt line it adds, the
apply_reporter_prior() backstop, and the webhook actually passing it through
(it used to be overwritten by the pre-submit classification unseen).
"""
import uuid

import chromadb
from fastapi.testclient import TestClient

from app import classifier, config, memory
from app.classifier import ReporterHint, apply_reporter_prior
from app.main import _reporter_hint_from, app
from app.models import ClassificationResult
from app.taxonomy import taxonomy

WEBHOOK_SECRET = "test-webhook-secret"
LEAF = "G01-S01"  # Feedback Too Generic or Vague
LEAF_NAME = taxonomy.get(LEAF)["name"]
GROUP = taxonomy.get(LEAF)["parent_id"]
GROUP_NAME = taxonomy.get(LEAF)["parent_name"]
OTHER_LEAF = next(i for i in taxonomy.category_ids if taxonomy.get(i)["parent_id"] != GROUP)


def _result(category_id, confidence, needs_clarification=False):
    return ClassificationResult(
        category_id=category_id,
        confidence=confidence,
        reasoning="model says so",
        needs_clarification=needs_clarification,
        clarifying_question="Which one?" if needs_clarification else None,
    )


# --- _reporter_hint_from ---------------------------------------------------

def test_subcategory_name_resolves_to_leaf_and_its_group():
    assert _reporter_hint_from(GROUP_NAME, LEAF_NAME) == ReporterHint(group_id=GROUP, leaf_id=LEAF)


def test_category_only_gives_group_hint():
    assert _reporter_hint_from(GROUP_NAME, None) == ReporterHint(group_id=GROUP, leaf_id=None)
    assert _reporter_hint_from(GROUP_NAME, "") == ReporterHint(group_id=GROUP, leaf_id=None)


def test_unresolvable_or_empty_pick_gives_no_hint():
    assert _reporter_hint_from(None, None) is None
    assert _reporter_hint_from("Not A Real Category", "Not a real subcategory") is None


def test_other_unclear_pick_gives_no_hint():
    fallback = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    assert _reporter_hint_from(fallback["parent_name"], fallback["name"]) is None
    assert _reporter_hint_from(fallback["parent_name"], None) is None


# --- prompt ------------------------------------------------------------------

def test_user_message_includes_reporter_pick_only_when_given():
    assert "REPORTER-SELECTED" not in classifier._build_user_message("text", None)
    msg = classifier._build_user_message("text", ReporterHint(group_id=GROUP, leaf_id=LEAF))
    assert "REPORTER-SELECTED" in msg and LEAF in msg and LEAF_NAME in msg
    group_msg = classifier._build_user_message("text", ReporterHint(group_id=GROUP))
    assert GROUP in group_msg and "no subcategory picked" in group_msg


# --- apply_reporter_prior ----------------------------------------------------

def test_agreement_skips_clarification_and_routes():
    out = apply_reporter_prior(_result(LEAF, 0.4, needs_clarification=True), ReporterHint(GROUP, LEAF))
    assert out.category_id == LEAF
    assert out.needs_clarification is False and out.clarifying_question is None
    assert out.confidence >= config.CONFIDENCE_THRESHOLD


def test_weak_disagreement_keeps_reporter_pick():
    out = apply_reporter_prior(_result(OTHER_LEAF, 0.7), ReporterHint(GROUP, LEAF))
    assert out.category_id == LEAF
    assert out.needs_clarification is False
    assert OTHER_LEAF in out.reasoning


def test_disagreement_wanting_clarification_keeps_reporter_pick():
    out = apply_reporter_prior(_result(OTHER_LEAF, 0.95, needs_clarification=True), ReporterHint(GROUP, LEAF))
    assert out.category_id == LEAF


def test_confident_disagreement_overrides_reporter_pick():
    out = apply_reporter_prior(
        _result(OTHER_LEAF, config.REPORTER_OVERRIDE_MIN_CONFIDENCE), ReporterHint(GROUP, LEAF)
    )
    assert out.category_id == OTHER_LEAF
    assert out.reasoning.startswith("Overrode the instructor's pick")


def test_group_only_or_no_hint_leaves_result_untouched():
    r = _result(OTHER_LEAF, 0.3, needs_clarification=True)
    assert apply_reporter_prior(r, None) == r
    assert apply_reporter_prior(r, ReporterHint(group_id=GROUP)) == r


# --- webhook -----------------------------------------------------------------

def test_suggestion_mode_passes_reporter_pick_to_classifier(monkeypatch):
    monkeypatch.setattr(config, "ZOHO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    seen = {}

    def fake_classify(text, api_key, embed_api_key=None, reporter_hint=None):
        seen["hint"] = reporter_hint
        return _result(LEAF, 0.9)

    monkeypatch.setattr(classifier, "classify", fake_classify)
    response = TestClient(app).post(
        "/api/webhooks/zoho/tickets",
        json={
            "issue_in_detail": "not helpful",
            "category_of_the_issue": GROUP_NAME,
            "sub_category_of_the_issue": LEAF_NAME,
        },
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )

    assert response.status_code == 200
    assert seen["hint"] == ReporterHint(group_id=GROUP, leaf_id=LEAF)
    assert response.json()["sub_category_of_the_issue"] == LEAF_NAME


def test_persist_mode_new_ticket_passes_reporter_pick(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ZOHO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    seen = {}

    def fake_classify(text, api_key, embed_api_key=None, reporter_hint=None):
        seen["hint"] = reporter_hint
        return _result(LEAF, 0.9)

    monkeypatch.setattr(classifier, "classify", fake_classify)
    TestClient(app).post(
        "/api/webhooks/zoho/tickets",
        json={
            "zoho_ticket_id": "Z-H1",
            "issue_in_detail": "not helpful",
            "category_of_the_issue": GROUP_NAME,
        },
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )

    assert seen["hint"] == ReporterHint(group_id=GROUP, leaf_id=None)


# --- failsafe when classify() fails --------------------------------------------

def _rate_limited(*a, **k):
    import httpx
    from openai import RateLimitError

    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    raise RateLimitError(
        "quota exhausted", response=httpx.Response(429, request=request), body=None
    )


def _post(payload):
    return TestClient(app).post(
        "/api/webhooks/zoho/tickets", json=payload, headers={"X-Webhook-Secret": WEBHOOK_SECRET}
    )


def _patch_keys(monkeypatch):
    monkeypatch.setattr(config, "ZOHO_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")


def test_suggestion_mode_key_exhausted_returns_instructor_pick_verbatim(monkeypatch):
    _patch_keys(monkeypatch)
    monkeypatch.setattr(classifier, "classify", _rate_limited)

    # Verbatim - even a value our taxonomy can't resolve is echoed back as-is.
    response = _post({
        "issue_in_detail": "not helpful",
        "category_of_the_issue": "Staffing & HR Lifecycle",
        "sub_category_of_the_issue": "Other_staffing & Hr Lifecycle",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["category_of_the_issue"] == "Staffing & HR Lifecycle"
    assert body["sub_category_of_the_issue"] == "Other_staffing & Hr Lifecycle"
    assert body["needs_review"] is True


def test_suggestion_mode_failure_without_subcategory_uses_fallback_leaf(monkeypatch):
    _patch_keys(monkeypatch)
    monkeypatch.setattr(classifier, "classify", _rate_limited)

    body = _post({"issue_in_detail": "not helpful", "category_of_the_issue": GROUP_NAME}).json()

    fallback = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    assert body["sub_category_of_the_issue"] == fallback["name"]
    assert body["needs_review"] is True


def test_persist_mode_key_exhausted_returns_and_routes_by_instructor_pick(isolated_db, monkeypatch):
    _patch_keys(monkeypatch)
    monkeypatch.setattr(classifier, "classify", _rate_limited)
    # Pre-submit failed too (key exhausted) - so On Add must try for real
    # rather than adopt the echoed instructor pick as a model answer.
    _post({"issue_in_detail": "not helpful", "category_of_the_issue": GROUP_NAME,
           "sub_category_of_the_issue": LEAF_NAME})

    body = _post({
        "zoho_ticket_id": "Z-F1",
        "issue_in_detail": "not helpful",
        "category_of_the_issue": GROUP_NAME,
        "sub_category_of_the_issue": LEAF_NAME,
    }).json()

    assert body["category_of_the_issue"] == GROUP_NAME
    assert body["sub_category_of_the_issue"] == LEAF_NAME
    assert body["needs_review"] is True
    ticket = isolated_db.get_ticket_by_zoho_id("Z-F1")
    assert ticket["category_id"] == LEAF
    assert ticket["status"] == "needs_human_review"
    assert ticket["fallback_reason"].startswith("classify_error:")


# --- memory ------------------------------------------------------------------

def test_retrieve_similar_adds_examples_from_reporter_categories(monkeypatch):
    collection = chromadb.Client().create_collection(
        name=f"test-{uuid.uuid4().hex[:12]}", metadata={"hnsw:space": "cosine"}
    )
    monkeypatch.setattr(memory, "_collection", collection)
    vectors = {
        "query": [1.0, 0.0],
        "close A": [1.0, 0.0],
        "close B": [0.99, 0.01],
        "reporter leaf example": [0.6, 0.4],
    }
    monkeypatch.setattr(memory, "_embed", lambda texts, api_key: [vectors[t] for t in texts])
    memory.add_example("close A", "cat-x", api_key="unused", source="seed")
    memory.add_example("close B", "cat-x", api_key="unused", source="seed")
    memory.add_example("reporter leaf example", "cat-reporter", api_key="unused", source="seed")

    plain = memory.retrieve_similar("query", api_key="unused", k=2, min_similarity=0.0)
    assert [r["category_id"] for r in plain] == ["cat-x", "cat-x"]

    widened = memory.retrieve_similar(
        "query", api_key="unused", k=2, min_similarity=0.0,
        also_from_categories=["cat-reporter"], also_k=2,
    )
    assert [r["category_id"] for r in widened] == ["cat-x", "cat-x", "cat-reporter"]


# --- On Add reuses the pre-submit classification -----------------------------

def _counting_classify(monkeypatch, result):
    calls = []

    def fake(text, api_key, embed_api_key=None, reporter_hint=None):
        calls.append(text)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(classifier, "classify", fake)
    return calls


def test_on_add_reuses_presubmit_result_without_a_second_call(isolated_db, monkeypatch):
    _patch_keys(monkeypatch)
    calls = _counting_classify(monkeypatch, _result(LEAF, 0.9))

    pre = _post({"issue_in_detail": "not helpful", "category_of_the_issue": GROUP_NAME}).json()
    _post({
        "zoho_ticket_id": "Z-P1", "issue_in_detail": "not helpful",
        "category_of_the_issue": pre["category_of_the_issue"],
        "sub_category_of_the_issue": pre["sub_category_of_the_issue"],
    })

    assert len(calls) == 1
    t = isolated_db.get_ticket_by_zoho_id("Z-P1")
    assert (t["status"], t["category_id"], t["confidence"]) == ("classified", LEAF, 0.9)
    assert t["reasoning"] == "model says so"


def test_on_add_without_remembered_suggestion_adopts_form_category(isolated_db, monkeypatch):
    _patch_keys(monkeypatch)
    calls = _counting_classify(monkeypatch, _result(OTHER_LEAF, 0.9))

    body = _post({
        "zoho_ticket_id": "Z-P2", "issue_in_detail": "not helpful",
        "category_of_the_issue": GROUP_NAME, "sub_category_of_the_issue": LEAF_NAME,
    }).json()

    assert calls == []
    assert (body["sub_category_of_the_issue"], body["needs_review"]) == (LEAF_NAME, False)
    t = isolated_db.get_ticket_by_zoho_id("Z-P2")
    assert (t["status"], t["category_id"], t["confidence"]) == ("classified", LEAF, None)


def test_on_add_keeps_form_value_changed_after_suggestion(isolated_db, monkeypatch):
    _patch_keys(monkeypatch)
    calls = _counting_classify(monkeypatch, _result(OTHER_LEAF, 0.9))

    _post({"issue_in_detail": "not helpful"})
    _post({
        "zoho_ticket_id": "Z-P3", "issue_in_detail": "not helpful",
        "category_of_the_issue": GROUP_NAME, "sub_category_of_the_issue": LEAF_NAME,
    })

    assert len(calls) == 1
    t = isolated_db.get_ticket_by_zoho_id("Z-P3")
    assert t["category_id"] == LEAF
    assert "changed on the Zoho form" in t["reasoning"]


def test_on_add_low_confidence_suggestion_goes_to_review(isolated_db, monkeypatch):
    _patch_keys(monkeypatch)
    _counting_classify(monkeypatch, _result(LEAF, 0.3))
    pre = _post({"issue_in_detail": "hmm"}).json()
    _post({"zoho_ticket_id": "Z-P4", "issue_in_detail": "hmm",
           "category_of_the_issue": pre["category_of_the_issue"],
           "sub_category_of_the_issue": pre["sub_category_of_the_issue"]})
    assert isolated_db.get_ticket_by_zoho_id("Z-P4")["status"] == "needs_human_review"


def test_on_add_classifies_when_presubmit_failed(isolated_db, monkeypatch):
    _patch_keys(monkeypatch)
    monkeypatch.setattr(classifier, "classify", _rate_limited)
    _post({"issue_in_detail": "not helpful", "category_of_the_issue": GROUP_NAME,
           "sub_category_of_the_issue": LEAF_NAME})

    calls = _counting_classify(monkeypatch, _result(LEAF, 0.9))
    _post({"zoho_ticket_id": "Z-P5", "issue_in_detail": "not helpful",
           "category_of_the_issue": GROUP_NAME, "sub_category_of_the_issue": LEAF_NAME})

    assert len(calls) == 1  # the first successful classification, not a repeat


def test_on_add_classifies_fallback_leaf_without_suggestion(isolated_db, monkeypatch):
    _patch_keys(monkeypatch)
    calls = _counting_classify(monkeypatch, _result(LEAF, 0.9))
    fallback = taxonomy.get(config.ZOHO_FALLBACK_CATEGORY_ID)
    _post({"zoho_ticket_id": "Z-P6", "issue_in_detail": "not helpful",
           "category_of_the_issue": fallback["parent_name"], "sub_category_of_the_issue": fallback["name"]})
    assert len(calls) == 1
