"""
Unit tests for app/memory.py's retrieval-quality fixes: the similarity
floor on retrieve_similar() and the per-ticket dedup on add_example().

Uses an ephemeral in-memory chromadb collection (swapped in for the real
module-level _collection) and a fake, deterministic _embed() so these never
call a real embeddings API and never touch the app's real persistent
Chroma store on disk.
"""
import uuid

import chromadb
import pytest

from app import memory


@pytest.fixture()
def isolated_memory(monkeypatch):
    client = chromadb.Client()  # ephemeral, in-process, no disk persistence
    # Unique name per test - chromadb.Client() reuses one process-wide
    # in-memory system, so a fixed collection name would collide across
    # tests (and error on the second create_collection call) instead of
    # giving each test a clean slate.
    collection = client.create_collection(name=f"test-{uuid.uuid4().hex[:12]}", metadata={"hnsw:space": "cosine"})
    monkeypatch.setattr(memory, "_collection", collection)

    # Fixed 2D vectors per exact text, so cosine similarity between any two
    # texts used in a test is exactly computable rather than depending on a
    # real embedding model.
    vectors = {
        "printer issue A": [1.0, 0.0],
        "printer issue B": [1.0, 0.0],   # same direction as A - "similar"
        "printer issue C": [0.99, 0.01],  # near-identical to A/B
        "billing question": [1.0, 0.0],
        "unrelated topic": [0.0, 1.0],   # orthogonal - "dissimilar"
    }

    def fake_embed(texts, api_key):
        return [vectors[t] for t in texts]

    monkeypatch.setattr(memory, "_embed", fake_embed)
    return collection


def test_retrieve_similar_drops_results_below_similarity_floor(isolated_memory):
    memory.add_example("billing question", "cat-billing", api_key="unused", source="seed")
    memory.add_example("unrelated topic", "cat-x", api_key="unused", source="seed")

    results = memory.retrieve_similar("printer issue A", api_key="unused", k=5, min_similarity=0.5)

    # "billing question" is an exact vector match (similarity 1.0) - kept.
    # "unrelated topic" is orthogonal (similarity 0.0) - dropped by the floor.
    assert [r["category_id"] for r in results] == ["cat-billing"]


def test_retrieve_similar_returns_up_to_k_after_filtering(isolated_memory):
    memory.add_example("printer issue A", "cat-printer", api_key="unused", source="seed")
    memory.add_example("printer issue B", "cat-printer", api_key="unused", source="seed")
    memory.add_example("unrelated topic", "cat-x", api_key="unused", source="seed")

    results = memory.retrieve_similar("printer issue C", api_key="unused", k=1, min_similarity=0.5)

    assert len(results) == 1
    assert results[0]["category_id"] == "cat-printer"


def test_add_example_with_same_ticket_id_replaces_prior_entry(isolated_memory):
    memory.add_example("printer issue A", "cat-wrong", api_key="unused", source="correction", ticket_id="ticket-1")
    assert isolated_memory.count() == 1

    # Same ticket corrected a second time, to a different category - the
    # stale "cat-wrong" entry must be gone, not left alongside the new one.
    memory.add_example("printer issue B", "cat-right", api_key="unused", source="correction", ticket_id="ticket-1")

    assert isolated_memory.count() == 1
    remaining = isolated_memory.get()
    assert remaining["metadatas"][0]["category_id"] == "cat-right"
    assert remaining["metadatas"][0]["ticket_id"] == "ticket-1"


def test_add_example_without_ticket_id_does_not_dedup(isolated_memory):
    """Seed examples (taxonomy.json's hand-written ones) have no ticket_id
    and should never be silently deleted by an unrelated add_example call."""
    memory.add_example("printer issue A", "cat-printer", api_key="unused", source="seed")
    memory.add_example("printer issue B", "cat-printer", api_key="unused", source="seed")

    assert isolated_memory.count() == 2
