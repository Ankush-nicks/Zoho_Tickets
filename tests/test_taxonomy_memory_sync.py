"""
Taxonomy edits reach the classifier's vector memory: a save on the Taxonomy
tab (and app startup) syncs the memory's seed examples to taxonomy.json,
keeping learned corrections and skipping the embeddings call when nothing
changed.
"""
import copy
import hashlib
import shutil

import pytest
from fastapi.testclient import TestClient

from app import config, main, memory
from app.main import app
from app.taxonomy import taxonomy


@pytest.fixture()
def temp_taxonomy(tmp_path):
    """Saves go to a copy of taxonomy.json; the real one is reloaded after."""
    original = taxonomy._path
    copy_path = tmp_path / "taxonomy.json"
    shutil.copy(original, copy_path)
    taxonomy._path = copy_path
    taxonomy.reload()
    yield
    taxonomy._path = original
    taxonomy.reload()


@pytest.fixture()
def fake_embeddings(monkeypatch):
    calls = []

    def fake(texts, api_key):
        calls.append(len(texts))
        return [[(b + 1) / 256 for b in hashlib.md5(t.encode()).digest()] for t in texts]

    monkeypatch.setattr(memory, "_embed", fake)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    return calls


def _seed_docs():
    return set(memory._collection.get(where={"source": "seed"})["documents"])


def _edited(new_example):
    data = {"version": taxonomy.version, "categories": copy.deepcopy(taxonomy.groups)}
    leaf = next(s for g in data["categories"] for s in g["subcategories"] if s["id"] == "G03-S05")
    removed = leaf["examples"].pop(0)
    leaf["examples"].append(new_example)
    return data, removed


def test_sync_seeds_an_empty_memory(temp_taxonomy, fake_embeddings):
    main._sync_seed_examples("test")
    assert memory.seeds_match(taxonomy.seed_examples())
    assert len(fake_embeddings) == 1


def test_sync_skips_embedding_when_examples_already_match(temp_taxonomy, fake_embeddings):
    main._sync_seed_examples("first")
    main._sync_seed_examples("second")
    assert len(fake_embeddings) == 1


def test_taxonomy_save_updates_memory_and_keeps_corrections(temp_taxonomy, fake_embeddings, monkeypatch):
    main._sync_seed_examples("startup")
    memory.add_example("learned from a correction", "G01-S03", "sk-test", ticket_id="t-1")
    started = []
    monkeypatch.setattr(main, "_start_seed_sync", lambda reason: started.append(reason) or "started")
    monkeypatch.setattr(main, "_require_taxonomy_password", lambda x: None)
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})

    data, removed = _edited("brand new example from the Taxonomy page")
    res = c.put("/api/taxonomy", json=data)
    assert res.status_code == 200 and res.json()["examples_sync"] == "started"
    assert started == ["taxonomy save"]

    main._sync_seed_examples("taxonomy save")  # what the background thread runs
    docs = _seed_docs()
    assert "brand new example from the Taxonomy page" in docs and removed not in docs
    assert memory.seeds_match(taxonomy.seed_examples())
    corrections = memory._collection.get(where={"source": "correction"})["documents"]
    assert corrections == ["learned from a correction"]


def test_definition_only_save_needs_no_embedding(temp_taxonomy, fake_embeddings):
    main._sync_seed_examples("startup")
    data = {"version": taxonomy.version, "categories": copy.deepcopy(taxonomy.groups)}
    data["categories"][0]["subcategories"][0]["description"] += " (edited)"
    taxonomy.save(data)
    main._sync_seed_examples("taxonomy save")
    assert len(fake_embeddings) == 1


def test_no_openai_key_skips_the_sync(temp_taxonomy, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(memory, "_embed", lambda *a: pytest.fail("must not embed without a key"))
    main._sync_seed_examples("test")
    assert _seed_docs() == set()
