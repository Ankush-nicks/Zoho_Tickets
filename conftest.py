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


@pytest.fixture(autouse=True)
def _isolated_memory_and_no_background_sync(monkeypatch):
    """
    Every test gets its own empty in-memory vector store, so nothing can
    reach the real app/data/chroma store (and, with a real OPENAI_API_KEY in
    .env, the real embeddings API through it). Saving the taxonomy starts a
    background memory sync (main._start_seed_sync) - a thread that could
    outlive the test's patches - so it's a no-op here; tests that cover the
    sync call main._sync_seed_examples directly.
    """
    import uuid

    import chromadb

    from app import main, memory

    collection = chromadb.Client().create_collection(
        name=f"test-{uuid.uuid4().hex[:12]}", metadata={"hnsw:space": "cosine"}
    )
    monkeypatch.setattr(memory, "_collection", collection)
    monkeypatch.setattr(main, "_start_seed_sync", lambda reason: "started")
