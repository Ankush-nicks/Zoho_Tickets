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


def add_example(text: str, category_id: str, api_key: str, source: str = "correction", example_id: str | None = None):
    """Add a confirmed/corrected example. This is what makes the system 'learn' over time."""
    import uuid
    embedding = _embed([text], api_key)[0]
    ex_id = example_id or f"{source}-{uuid.uuid4().hex[:12]}"
    _collection.add(
        ids=[ex_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[{"category_id": category_id, "source": source}],
    )


def retrieve_similar(text: str, api_key: str, k: int = config.FEWSHOT_K) -> list[dict]:
    """Return the k most similar known examples to use as dynamic few-shot context."""
    if is_empty():
        return []
    query_embedding = _embed([text], api_key)[0]
    results = _collection.query(
        query_embeddings=[query_embedding],
        n_results=min(k, max(_collection.count(), 1)),
    )
    out = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({
            "text": doc,
            "category_id": meta.get("category_id"),
            "source": meta.get("source"),
            "similarity": 1 - dist,  # cosine distance -> similarity
        })
    return out
