import pytest

from app import config, db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """
    Points app.db at a throwaway SQLite file for the duration of every test,
    so tests never read or write the real local tickets.db (or a real remote
    Turso database, if TURSO_DATABASE_URL happens to be set in this
    environment's .env - db.USE_TURSO is computed once at import time from
    that env var, so it must be forced False here too, not just
    config.SQLITE_PATH, or these "isolated" tests would silently hit
    production data over the network). Autouse because even the Zoho
    webhook's pre-submit call writes now (a draft row), so no test is safe
    without it; tests that need the module itself still request it by name.

    db.list_all_tickets() also memoizes its result in a module-level dict
    for 20 seconds, invalidated only by create_ticket/update_ticket - so it
    must be explicitly cleared here too, or a test could see stale data
    cached by a previous test's (now swapped-out) database.
    """
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "test_tickets.db")
    monkeypatch.setattr(db, "USE_TURSO", False)
    db._invalidate_list_all_cache()
    db.init_db()
    yield db
