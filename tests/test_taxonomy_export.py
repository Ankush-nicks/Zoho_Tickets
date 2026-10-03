"""
Taxonomy CSV download: every subcategory with all of its fields, each
example in its own column.
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.taxonomy import taxonomy


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin")
    c = TestClient(app)
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    return c


def test_export_includes_every_field_and_examples(client):
    res = client.get("/api/taxonomy/export.csv")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(res.content.decode("utf-8-sig"))))

    subs = [(g, s) for g in taxonomy.groups for s in g.get("subcategories", [])]
    assert len(rows) == len(subs)
    most = max(len(s.get("examples", [])) for _, s in subs)
    assert [k for k in rows[0] if k.startswith("example_") and k != "example_count"] == [f"example_{i}" for i in range(1, most + 1)]
    for row, (group, sub) in zip(rows, subs):
        assert row["category_id"] == group["id"]
        assert row["category_name"] == group["name"]
        assert row["subcategory_id"] == sub["id"]
        assert row["subcategory_name"] == sub["name"]
        assert row["description"] == sub.get("description", "")
        assert row["assigned_team"] == sub.get("assigned_team", "")
        assert row["poc_primary"] == sub.get("poc_primary", "")
        examples = sub.get("examples", [])
        assert row["example_count"] == str(len(examples))
        got = [row[k] for k in row if k.startswith("example_") and k != "example_count"]
        assert got[:len(examples)] == examples
        assert all(v == "" for v in got[len(examples):])


def test_export_requires_login():
    res = TestClient(app).get("/api/taxonomy/export.csv")
    assert res.status_code == 401
