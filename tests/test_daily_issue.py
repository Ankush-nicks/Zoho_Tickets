"""
Daily Issue Check tab: ticket rows from the database, the page route, and
the OpenRouter-backed AI endpoints (text streaming + JSON mode).
"""
import gzip
import json
import types

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import RateLimitError

from app import config, daily_issue
from app.main import app


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-test")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


def _ticket(isolated_db, zid, **kw):
    tid = isolated_db.create_ticket(
        kw.pop("text", "Recording not saved"), zoho_ticket_id=zid,
        zoho_category=kw.pop("zoho_category", None), zoho_subcategory=kw.pop("zoho_subcategory", None),
        raw_payload=kw.pop("raw", {}), created_at=kw.pop("created_at", 1788237000.0),
    )
    if kw:
        isolated_db.update_ticket(tid, **kw)
    return tid


# --- rows -----------------------------------------------------------------------

def test_row_shape_uses_taxonomy_leaf_and_raw_fields(isolated_db):
    _ticket(isolated_db, "3591", category_id="G01-S01", raw={
        "ticket_status": "Yet To Pick", "subject_name": "Fullstack", "university_boa": "GMR  Institute",
        "sla_breach_status": "Breached", "is_it_a_recurring_issue": "Yes", "session_type": "Lab",
        "resolution_by_the_poc": "Fixed\nit", "ticket_reopen_count": "2", "assigned_team": "IAS",
        "ticket_closure_date_time": "30/09/2026 15:01:02",
    })
    [row] = daily_issue.build_rows(isolated_db.list_all_tickets())
    assert row == [
        3591, "Yet To Pick", "QA Report / Instructor Evaluation", "Feedback Too Generic or Vague",
        "Fullstack", "GMR Institute", "2026-09-01T10:00:00", "Breached", "Yes", "Lab",
        "Recording not saved", "Fixed it", 2, "IAS", "2026-09-30T15:01:02",
    ]


def test_row_falls_back_to_zoho_category_and_tolerates_missing_fields(isolated_db):
    _ticket(isolated_db, "77", zoho_category="Facilities & Equipment", zoho_subcategory="Laptop / Device Issue",
            raw={"ticket_reopen_count": "n/a", "university": "Aurora"})
    [row] = daily_issue.build_rows(isolated_db.list_all_tickets())
    assert row[2:4] == ["Facilities & Equipment", "Laptop / Device Issue"]
    assert row[5] == "Aurora" and row[12] == 0 and row[14] == ""


def test_rows_skip_non_zoho_and_uncategorised_tickets(isolated_db):
    _ticket(isolated_db, None)                           # manual ticket, no Zoho id
    _ticket(isolated_db, "abc", category_id="G01-S01")   # non-numeric id
    _ticket(isolated_db, "12")                           # no category anywhere
    assert daily_issue.build_rows(isolated_db.list_all_tickets()) == []


def test_text_is_clipped_like_the_page_did(isolated_db):
    _ticket(isolated_db, "5", category_id="G01-S01", text="x " * 2000, raw={"resolution_by_the_poc": "y" * 1000})
    [row] = daily_issue.build_rows(isolated_db.list_all_tickets())
    assert len(row[10]) == 900 and len(row[11]) == 400


# --- routes -----------------------------------------------------------------------

def test_page_and_data_need_login(isolated_db):
    c = TestClient(app)
    assert c.get("/daily-issue-check", follow_redirects=False).status_code in (302, 307)
    assert c.get("/api/daily-issue/tickets").status_code == 401
    assert c.post("/api/daily-issue/ai/json", json={"prompt": "x"}).status_code == 401


def test_page_is_served_without_embedded_ticket_data(client):
    r = client.get("/daily-issue-check")
    assert r.status_code == 200
    assert 'id="seed"' not in r.text and "window.claude" not in r.text
    assert "/api/daily-issue/tickets" in r.text


def test_tickets_endpoint_gzips_and_reports_ai(client, isolated_db):
    _ticket(isolated_db, "1", category_id="G01-S01")
    r = client.get("/api/daily-issue/tickets", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    body = r.json()  # httpx decodes gzip transparently
    assert body["ai"] is True and body["model"] == config.DAILY_ISSUE_MODEL
    assert [row[0] for row in body["rows"]] == [1]


# --- AI ---------------------------------------------------------------------------

def _chunk(text=None, finish=None):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        delta=types.SimpleNamespace(content=text), finish_reason=finish)])


def _fake_client(monkeypatch, create):
    fake = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(daily_issue, "_client", lambda: fake)


def test_text_streams_and_marks_truncation(client, monkeypatch):
    seen = {}

    def create(**kw):
        seen.update(kw)
        return iter([_chunk("Hello "), _chunk("world"), _chunk(None, "length")])

    _fake_client(monkeypatch, create)
    r = client.post("/api/daily-issue/ai/text", json={"prompt": "Summarize"})
    assert r.status_code == 200
    assert r.text == "Hello world" + daily_issue.STREAM_END + "truncated"
    assert seen["model"] == config.DAILY_ISSUE_MODEL and seen["stream"] is True


def test_json_mode_returns_object_and_rejects_bad_json(client, monkeypatch):
    replies = iter(['{"groups":[{"name":"A","rule":"r"}]}', "not json"])

    def create(**kw):
        assert kw["response_format"] == {"type": "json_object"}
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=next(replies)))])

    _fake_client(monkeypatch, create)
    assert client.post("/api/daily-issue/ai/json", json={"prompt": "Reply JSON"}).json() == {"groups": [{"name": "A", "rule": "r"}]}
    bad = client.post("/api/daily-issue/ai/json", json={"prompt": "Reply JSON"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_json"


def test_rate_limit_becomes_page_error_code(client, monkeypatch):
    def create(**kw):
        req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        raise RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)

    _fake_client(monkeypatch, create)
    for path in ("/api/daily-issue/ai/text", "/api/daily-issue/ai/json"):
        r = client.post(path, json={"prompt": "x"})
        assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


def test_ai_off_without_key_and_rejects_empty_or_huge_prompts(client, monkeypatch):
    assert client.post("/api/daily-issue/ai/json", json={"prompt": ""}).json()["detail"]["code"] == "empty_prompt"
    huge = client.post("/api/daily-issue/ai/json", json={"prompt": "x" * (daily_issue.PROMPT_MAX_CHARS + 1)})
    assert huge.status_code == 413
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", None)
    r = client.post("/api/daily-issue/ai/text", json={"prompt": "x"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "not_configured"
    assert client.get("/api/daily-issue/tickets").json()["ai"] is False
