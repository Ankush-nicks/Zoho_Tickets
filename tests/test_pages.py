"""
The app has two pages, Log and Taxonomy. Removed pages' old addresses
send people to the Log, and login only returns to the two that exist.
"""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

REMOVED = ("/daily", "/pulse", "/stats", "/drill-down", "/tickets", "/daily-issue", "/daily-issue-check")


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


@pytest.mark.parametrize("path", ["/", "/log", "/classify", "/taxonomy"])
def test_pages_serve_the_shell(client, path):
    res = client.get(path, follow_redirects=False)
    assert res.status_code == 200
    assert 'id="navBtn-log"' in res.text and 'id="navBtn-taxonomy"' in res.text
    assert 'id="navBtn-classify"' in res.text
    for gone in ("navBtn-daily", "navBtn-pulse", "navBtn-stats", "navBtn-dailyissue"):
        assert gone not in res.text


@pytest.mark.parametrize("path", REMOVED)
def test_removed_pages_go_to_the_log(client, path):
    res = client.get(path, follow_redirects=False)
    assert res.status_code in (302, 307) and res.headers["location"] == "/log"


def test_login_returns_only_to_existing_pages():
    anon = TestClient(app)
    assert anon.get("/taxonomy", follow_redirects=False).headers["location"] == "/login?next=/taxonomy"
    assert anon.get("/", follow_redirects=False).headers["location"] == "/login"


def test_drill_down_api_is_gone(client):
    assert client.get("/api/daily-issue/tickets").status_code == 404
