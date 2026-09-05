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
    def version(self):
        return self._data.get("version")

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

    def save(self, data: dict) -> None:
        """
        Validates and persists a full taxonomy replacement (same {version,
        categories: [...]} shape reload() expects - the Taxonomy tab's editor
        sends its whole working copy back on Save, not a per-field patch).
        Raises ValueError on structural problems (missing ids/names,
        duplicate ids) before ever touching disk. Reloads immediately after
        writing so classify()/grade_resolution() pick up the change without
        a restart.
        """
        if not isinstance(data.get("categories"), list) or not data["categories"]:
            raise ValueError("taxonomy must have a non-empty 'categories' list")

        seen_cat_ids: set[str] = set()
        seen_sub_ids: set[str] = set()
        for group in data["categories"]:
            cat_id, cat_name = group.get("id"), group.get("name")
            if not cat_id or not cat_name:
                raise ValueError(f"every category needs an id and name (got {group!r})")
            if cat_id in seen_cat_ids:
                raise ValueError(f"duplicate category id: {cat_id}")
            seen_cat_ids.add(cat_id)
            for sub in group.get("subcategories", []):
                sub_id, sub_name = sub.get("id"), sub.get("name")
                if not sub_id or not sub_name:
                    raise ValueError(f"every subcategory needs an id and name (got {sub!r})")
                if sub_id in seen_sub_ids:
                    raise ValueError(f"duplicate subcategory id: {sub_id}")
                seen_sub_ids.add(sub_id)

        with self._lock:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        self.reload()

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
