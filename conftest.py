import pytest

from app import config, db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """
    Points app.db at a throwaway SQLite file for the duration of one test, so
    tests never read or write the real local tickets.db (or a real remote
    Turso database, if TURSO_DATABASE_URL happens to be set in this
    environment's .env - db.USE_TURSO is computed once at import time from
    that env var, so it must be forced False here too, not just
    config.SQLITE_PATH, or these "isolated" tests would silently hit
    production data over the network).
    """
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "test_tickets.db")
    monkeypatch.setattr(db, "USE_TURSO", False)
    db.init_db()
    yield db
