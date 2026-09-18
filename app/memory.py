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


def _embed(texts: list[str], api_key: str) -> list[list[float]]:
    client = OpenAI(api_key=api_key, base_url=config.openai_base_url())
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


def retrieve_similar(
    text: str,
    api_key: str,
    k: int = config.FEWSHOT_K,
    min_similarity: float = config.FEWSHOT_MIN_SIMILARITY,
) -> list[dict]:
    """
    Return up to k most similar known examples to use as dynamic few-shot
    context, dropping any whose similarity falls below min_similarity - a
    genuinely novel ticket should get few or zero examples rather than k
    forced "closest but irrelevant" ones. Over-fetches (k*3, capped at the
    collection size) before filtering so a few weak matches interspersed
    among the nearest neighbors don't silently shrink the result below k.
    """
    if is_empty():
        return []
    query_embedding = _embed([text], api_key)[0]
    fetch_n = min(k * 3, max(_collection.count(), 1))
    results = _collection.query(
        query_embeddings=[query_embedding],
        n_results=fetch_n,
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
        if len(out) >= k:
            break
    return out
