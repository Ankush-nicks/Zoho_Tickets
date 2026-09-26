"""
Refresh the vector memory's seed examples from the current taxonomy.json.

app/memory.py's seed_if_empty() only seeds an empty store, so after editing
examples in taxonomy.json (by hand or via the Taxonomy tab) the running
service keeps retrieving the OLD seed examples. This replaces just the
seed entries (source="seed") and leaves every real correction in place -
unlike deleting app/data/chroma, which would lose them all.

Must run where the service's Chroma store lives (config.CHROMA_PATH), i.e.
on the deployed instance, not a local checkout. One embeddings call for
all seed examples.

Usage:
    OPENAI_API_KEY=sk-... python scripts/reseed_memory.py
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, memory  # noqa: E402
from app.taxonomy import taxonomy  # noqa: E402


def main() -> int:
    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY is not set - needed to embed the seed examples.")
        return 1
    before = Counter(m.get("source") for m in memory._collection.get(include=["metadatas"])["metadatas"])
    print(f"Chroma store: {config.CHROMA_PATH}")
    print(f"Before: {dict(before)}")
    removed, added = memory.reseed(taxonomy.seed_examples(), config.OPENAI_API_KEY)
    after = Counter(m.get("source") for m in memory._collection.get(include=["metadatas"])["metadatas"])
    print(f"Removed {removed} old seed examples, added {added} from taxonomy.json.")
    print(f"After:  {dict(after)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
