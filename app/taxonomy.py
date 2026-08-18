import json
import threading
from typing import Any

from . import config


class Taxonomy:
    """
    Wraps taxonomy.json (2 levels: category group -> subcategory).
    Classification targets the LEAF (subcategory) level, since that's what
    actually determines routing (assigned_team / poc_primary / poc_cc) in
    real taxonomy exports. Loaded once at startup, cached in memory, but
    re-readable via reload() so the taxonomy can be edited/hot-swapped
    without restarting the service.
    """

    def __init__(self, path=config.TAXONOMY_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._leaf_index: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)

            leaf_index: dict[str, dict] = {}
            for group in data["categories"]:
                for sub in group.get("subcategories", []):
                    if sub["id"] in leaf_index:
                        raise ValueError(f"Duplicate subcategory id in taxonomy.json: {sub['id']}")
                    entry = dict(sub)
                    entry["parent_id"] = group["id"]
                    entry["parent_name"] = group["name"]
                    leaf_index[sub["id"]] = entry

            self._data = data
            self._leaf_index = leaf_index

    @property
    def groups(self) -> list[dict]:
        """Top-level category groups, each containing its subcategories."""
        return self._data["categories"]

    @property
    def category_ids(self) -> list[str]:
        """Leaf (subcategory) ids - what the classifier actually predicts."""
        return list(self._leaf_index.keys())

    def get(self, category_id: str) -> dict | None:
        """Look up a leaf subcategory by id. Includes parent_id/parent_name,
        assigned_team, poc_primary, poc_cc for routing."""
        return self._leaf_index.get(category_id)

    def seed_examples(self) -> list[dict]:
        """
        Flatten every leaf's hand-written/historical examples into
        (text, category_id) pairs used to bootstrap the vector memory
        before any real corrected tickets exist.
        """
        out = []
        for leaf_id, leaf in self._leaf_index.items():
            for ex in leaf.get("examples", []):
                out.append({"text": ex, "category_id": leaf_id, "source": "seed"})
        return out

    def as_prompt_block(self) -> str:
        """Render the taxonomy as text for the classification system prompt,
        grouped by parent category so related subcategories stay together."""
        lines = []
        for group in self.groups:
            lines.append(f"## {group['name']} ({group['id']})")
            for sub in group.get("subcategories", []):
                lines.append(f"- id: {sub['id']}\n  name: {sub['name']}\n  description: {sub['description']}")
        return "\n".join(lines)


taxonomy = Taxonomy()
