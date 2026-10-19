"""
The Log page refreshes itself after an On Add call. It polls
/api/ticket-log-stamp, which changes whenever an On Add call finishes and
never reads the database (it is polled often, and the database is on a
free tier).
"""
import pytest
from fastapi.testclient import TestClient

from app import config, db as app_db
from app.main import app
from tests.test_routing_log import LEAF, _model_says, _on_add, _presubmit


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


def _stamp(client):
    res = client.get("/api/ticket-log-stamp")
    assert res.status_code == 200
    return res.json()["stamp"]


def test_on_add_changes_the_stamp(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    before = _stamp(client)
    _on_add("Z-1", _presubmit())
    assert _stamp(client) != before


def test_presubmit_alone_does_not_change_it(client, isolated_db, monkeypatch):
    _model_says(monkeypatch, LEAF, 0.9)
    before = _stamp(client)
    _presubmit()
    assert _stamp(client) == before


def test_stamp_never_reads_the_database(client, isolated_db, monkeypatch):
    def boom():
        raise AssertionError("the stamp must not touch the database")

    monkeypatch.setattr(app_db, "_conn", boom)
    _stamp(client)


def test_stamp_requires_login(isolated_db):
    assert TestClient(app).get("/api/ticket-log-stamp").status_code == 401
