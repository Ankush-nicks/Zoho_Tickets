import copy
import json
import logging
import threading
import time
from typing import Any

from . import config, db


class TaxonomyVersionConflict(ValueError):
    """save(expected_version=...) found the live copy at a different version."""


class Taxonomy:
    """
    Wraps taxonomy.json (2 levels: category group -> subcategory).
    Classification targets the LEAF (subcategory) level, since that's what
    actually determines routing (assigned_team / poc_primary) in
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
        with open(self._path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._apply(data)

    def _apply(self, data: dict) -> None:
        leaf_index: dict[str, dict] = {}
        for group in data["categories"]:
            for sub in group.get("subcategories", []):
                if sub["id"] in leaf_index:
                    raise ValueError(f"Duplicate subcategory id in taxonomy: {sub['id']}")
                entry = dict(sub)
                entry["parent_id"] = group["id"]
                entry["parent_name"] = group["name"]
                leaf_index[sub["id"]] = entry

        with self._lock:
            self._data = data
            self._leaf_index = leaf_index

    @classmethod
    def from_data(cls, data: dict) -> "Taxonomy":
        """A standalone copy built from `data` - never touches disk or the database."""
        t = cls.__new__(cls)
        t._path = None
        t._lock = threading.Lock()
        t._data = {}
        t._leaf_index = {}
        t._apply(copy.deepcopy(data))
        return t

    def as_dict(self) -> dict:
        """Deep copy of {version, categories} - safe to edit, then save()."""
        return copy.deepcopy({"version": self._data.get("version"), "categories": self._data["categories"]})

    def load_live(self) -> str:
        """Loads the starting copy (taxonomy.json), then the database's live
        copy unless the file's version is higher (a deploy shipped a newer
        one) or the database has none - in which case the file is written to
        the database. Returns "db" or "file"."""
        log = logging.getLogger("uvicorn.error")
        self.reload()
        file_data = self.as_dict()
        file_v = file_data.get("version") or 0
        doc = db.get_taxonomy_doc()
        if doc is None:
            log.info("taxonomy: file v%s seeded empty db", file_v)
            db.put_taxonomy_doc(file_data)
            return "file"
        db_v = doc.get("version") or 0
        if file_v > db_v:
            log.info("taxonomy: file v%s replaced db v%s", file_v, db_v)
            db.put_taxonomy_doc_as(f"previous-{int(time.time())}", doc)
            db.put_taxonomy_doc(file_data)
            return "file"
        log.info("taxonomy: db v%s kept (file v%s)", db_v, file_v)
        self._apply(doc)
        return "db"

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
        assigned_team, poc_primary for routing."""
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

    def save(self, data: dict, expected_version=None) -> None:
        """
        Validates and persists (to the database; bumps the version) a full taxonomy replacement (same {version,
        categories: [...]} shape reload() expects - the Taxonomy tab's editor
        sends its whole working copy back on Save, not a per-field patch).
        Raises ValueError on structural problems (missing ids/names,
        duplicate ids) before ever touching the database. Reloads immediately after
        writing so classify() picks up the change without
        a restart.
        """
        if expected_version is not None and expected_version != self.version:
            raise TaxonomyVersionConflict("taxonomy version changed")
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

        data["version"] = (self.version or 0) + 1
        db.put_taxonomy_doc(data)
        self._apply(copy.deepcopy(data))

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
