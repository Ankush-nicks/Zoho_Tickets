"""
Dynamic context memory.

Every classification call pulls the K most similar known-good examples
(seed examples from taxonomy.json + real corrected tickets) and injects
them into the prompt as few-shot context. When an agent corrects a
prediction, that (ticket_text -> correct_category) pair is embedded and
added here, so the *next* similar ticket benefits from the correction
immediately - no retraining, no redeploy.
"""

import chromadb
from openai import OpenAI

from . import config

_chroma = chromadb.PersistentClient(
    path=config.CHROMA_PATH,
    settings=chromadb.Settings(anonymized_telemetry=False),
)
_collection = _chroma.get_or_create_collection(
    name="ticket_examples",
    metadata={"hnsw:space": "cosine"},
)


# The embedding model rejects inputs over 8,191 tokens. Real Zoho tickets go
# up to ~45K characters (pasted logs/transcripts) - uncapped, those fail on
# every classify attempt and sit at the head of the pending queue forever.
# 8,000 characters stays under the token cap even for non-Latin text, and
# the opening of a ticket is what similarity search actually needs.
_EMBED_MAX_CHARS = 8000


def _embed(texts: list[str], api_key: str) -> list[list[float]]:
    client = OpenAI(api_key=api_key, base_url=config.openai_base_url())
    texts = [t[:_EMBED_MAX_CHARS] for t in texts]
    resp = client.embeddings.create(model=config.EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def is_empty() -> bool:
    return _collection.count() == 0


def seed_if_empty(seed_examples: list[dict], api_key: str):
    """Bootstrap memory from taxonomy.json's hand-written examples on first run."""
    if not is_empty() or not seed_examples:
        return
    texts = [e["text"] for e in seed_examples]
    embeddings = _embed(texts, api_key)
    ids = [f"seed-{i}" for i in range(len(texts))]
    metadatas = [{"category_id": e["category_id"], "source": "seed"} for e in seed_examples]
    _collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)


def reseed(seed_examples: list[dict], api_key: str) -> tuple[int, int]:
    """
    Replace every seed example with the current taxonomy.json ones, leaving
    corrections untouched. seed_if_empty() only ever runs on an empty store,
    so edits to taxonomy.json's examples never reach retrieval otherwise -
    and wiping app/data/chroma to force a re-seed would also throw away
    every real correction. Embeds before deleting, so a failed embeddings
    call leaves the old seeds in place rather than an empty seed set.
    Returns (removed, added).
    """
    texts = [e["text"] for e in seed_examples]
    embeddings = _embed(texts, api_key) if texts else []
    old_ids = _collection.get(where={"source": "seed"}, include=[])["ids"]
    if old_ids:
        _collection.delete(ids=old_ids)
    if texts:
        ids = [f"seed-{i}" for i in range(len(texts))]
        metadatas = [{"category_id": e["category_id"], "source": "seed"} for e in seed_examples]
        _collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    return len(old_ids), len(texts)


def add_example(
    text: str,
    category_id: str,
    api_key: str,
    source: str = "correction",
    example_id: str | None = None,
    ticket_id: str | None = None,
):
    """
    Add a confirmed/corrected example. This is what makes the system 'learn' over time.

    ticket_id should be passed for every real correction (see app/main.py's
    correct_ticket) so that correcting the same ticket a second time - a POC
    picks category B, then later realizes it's actually C - replaces the
    stale B example instead of leaving both B and C in memory forever,
    quietly contradicting each other in future retrievals.
    """
    import uuid

    if ticket_id:
        _collection.delete(where={"ticket_id": ticket_id})

    embedding = _embed([text], api_key)[0]
    ex_id = example_id or f"{source}-{uuid.uuid4().hex[:12]}"
    metadata = {"category_id": category_id, "source": source}
    if ticket_id:
        metadata["ticket_id"] = ticket_id
    _collection.add(
        ids=[ex_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata],
    )


def _query(query_embedding: list[float], n: int, min_similarity: float, where: dict | None = None) -> list[dict]:
    fetch_n = min(n * 3, max(_collection.count(), 1))
    results = _collection.query(
        query_embeddings=[query_embedding],
        n_results=fetch_n,
        where=where,
    )
    out = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        similarity = 1 - dist  # cosine distance -> similarity
        if similarity < min_similarity:
            continue
        out.append({
            "text": doc,
            "category_id": meta.get("category_id"),
            "source": meta.get("source"),
            "similarity": similarity,
        })
        if len(out) >= n:
            break
    return out


def retrieve_similar(
    text: str,
    api_key: str,
    k: int = config.FEWSHOT_K,
    min_similarity: float = config.FEWSHOT_MIN_SIMILARITY,
    also_from_categories: list[str] | None = None,
    also_k: int = config.FEWSHOT_REPORTER_K,
) -> list[dict]:
    """
    Return up to k most similar known examples to use as dynamic few-shot
    context, dropping any whose similarity falls below min_similarity - a
    genuinely novel ticket should get few or zero examples rather than k
    forced "closest but irrelevant" ones. Over-fetches (k*3, capped at the
    collection size) before filtering so a few weak matches interspersed
    among the nearest neighbors don't silently shrink the result below k.

    also_from_categories (the leaf ids around what the instructor picked on
    the Zoho form - see classifier.ReporterHint) additionally pulls up to
    also_k of the most similar examples restricted to just those leaves,
    appended after the top-k and deduped against it. Without this, a short
    ticket like "not working" retrieves whatever sounds closest globally,
    and the model never sees what the reporter's own subcategory - or its
    near siblings - actually look like. Reuses the same query embedding, so
    this costs one extra Chroma query, not an extra embeddings call.
    """
    if is_empty():
        return []
    query_embedding = _embed([text], api_key)[0]
    out = _query(query_embedding, k, min_similarity)
    if also_from_categories and also_k > 0:
        seen = {ex["text"] for ex in out}
        where = (
            {"category_id": also_from_categories[0]}
            if len(also_from_categories) == 1
            else {"category_id": {"$in": also_from_categories}}
        )
        try:
            extra = _query(query_embedding, also_k + len(out), min_similarity, where=where)
        except Exception:
            # A filtered HNSW query can fail on some chromadb versions when
            # very few rows match - the unfiltered top-k is still usable.
            extra = []
        out.extend([ex for ex in extra if ex["text"] not in seen][:also_k])
    return out
