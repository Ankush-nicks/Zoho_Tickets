import copy
import os

from app import config, db
from app.taxonomy import Taxonomy, taxonomy


def _file_doc():
    t = Taxonomy(config.TAXONOMY_PATH)
    return t.as_dict()


def test_save_writes_db_not_file():
    before = os.stat(config.TAXONOMY_PATH).st_mtime_ns
    d = taxonomy.as_dict()
    d["categories"][0]["subcategories"][0]["description"] = "edited via save"
    taxonomy.save(d)
    assert db.get_taxonomy_doc()["categories"] == d["categories"]
    assert os.stat(config.TAXONOMY_PATH).st_mtime_ns == before


def test_save_bumps_version():
    taxonomy.load_live()
    v = taxonomy.version
    taxonomy.save(taxonomy.as_dict())
    assert taxonomy.version == v + 1
    assert db.get_taxonomy_doc()["version"] == v + 1


def test_load_live_uses_db_copy():
    doc = _file_doc()
    sub = doc["categories"][0]["subcategories"][0]
    sub["description"] = "edited in db"
    db.put_taxonomy_doc(doc)
    assert taxonomy.load_live() == "db"
    assert taxonomy.get(sub["id"])["description"] == "edited in db"


def test_startup_prefers_newer_file():
    doc = _file_doc()
    file_version = doc["version"]
    doc["version"] = file_version - 1
    db.put_taxonomy_doc(doc)
    assert taxonomy.load_live() == "file"
    assert db.get_taxonomy_doc()["version"] == file_version


def test_load_live_seeds_empty_db():
    assert db.get_taxonomy_doc() is None
    assert taxonomy.load_live() == "file"
    assert db.get_taxonomy_doc() is not None


def test_from_data_is_independent():
    snapshot = copy.deepcopy(taxonomy.as_dict())
    other = Taxonomy.from_data(taxonomy.as_dict())
    other.groups[0]["subcategories"][0]["description"] = "changed"
    other.groups[0]["name"] = "changed"
    assert taxonomy.as_dict() == snapshot
