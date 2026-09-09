# POC Ticket Queue Extension (Step 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a read-only Chrome extension popup showing `ranjith.kumar@nxtwave.co.in`'s open ticket queue (acknowledgement + SLA status per ticket), backed by one new additive FastAPI endpoint.

**Architecture:** A new pure-logic module (`app/poc_queue.py`) resolves a POC's open tickets and computes ack/SLA state from existing `tickets` rows plus a small new category→SLA-hours config table; a thin FastAPI route (`GET /api/extension/my-tickets`, header-token auth) exposes it as JSON; a Manifest V3 popup fetches it once on open and renders/sorts client-side.

**Tech Stack:** Python 3 / FastAPI (existing backend), pytest + FastAPI `TestClient` (new — this repo has no test suite yet), vanilla JS/HTML/CSS Chrome extension (Manifest V3, no build step).

**Spec:** `docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md`

## Global Constraints

- Do not modify `app/classifier.py`, `app/taxonomy.py`, `app/taxonomy.json`, or any existing route/table. This is strictly additive.
- `acknowledged_at` stays `NULL` for every ticket in this step — no writer is added. Every ticket will show as not-yet-acknowledged; this is a known, accepted limitation (spec §2).
- No "Open in Zoho" link/button/action anywhere in this step (spec §2) — not built disabled, simply absent.
- "Closed" has exactly one definition across the codebase: reuse `quality_scorer.CLOSED_STATUSES` (`{"Resolved By POC", "Resolution Acknowledged"}`). Do not define a second set.
- POC matching on `taxonomy.json`'s `poc_primary` field is containment-in-a-comma-separated-list (case-insensitive, trimmed), never exact string equality — that field is free text and sometimes lists several people.
- SLA hours come only from a new `config.CATEGORY_SLA_HOURS` table keyed by top-level taxonomy group code (only `"G01": 24.0` populated — the only group reachable by this POC). Zoho's `priority_level` is displayed as-is but never drives SLA hours; Zoho supplies no SLA deadline of its own.
- Extension: no write actions, no background polling/badge counts, no content scripts injected into Zoho, no login UI (one extension = one POC = one baked-in token).

---

### Task 1: `acknowledged_at` column + test harness

**Files:**
- Modify: `app/db.py:44-45` (SCHEMA), `app/db.py:143-144` (init_db ALTER loop)
- Create: `pytest.ini`
- Create: `conftest.py` (repository root — guarantees `app` is importable from `tests/`)
- Create: `requirements-dev.txt`
- Create: `tests/test_db_acknowledged_at.py`

**Interfaces:**
- Produces: a nullable `acknowledged_at REAL` column on `tickets`, readable/writable through the existing `db.create_ticket()` / `db.update_ticket(ticket_id, **fields)` / `db.get_ticket(ticket_id)` — no new `db.py` functions.
- Produces: an `isolated_db` pytest fixture (in root `conftest.py`) that every later task's tests use — points `config.SQLITE_PATH` at a throwaway file and calls `db.init_db()`.

- [ ] **Step 1: Add pytest tooling and the root conftest**

Create `requirements-dev.txt`:

```
-r requirements.txt
pytest==8.3.3
```

Create `pytest.ini`:

```ini
[pytest]
testpaths = tests
```

Create `conftest.py` at the repository root (same level as `app/`, `requirements.txt`):

```python
import pytest

from app import config, db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """
    Points app.db at a throwaway SQLite file for the duration of one test, so
    tests never read or write the real local tickets.db. config.SQLITE_PATH
    is read fresh by db._conn() on every call (never cached into a module-
    level variable), so monkeypatching it here is enough - no need to touch
    db.USE_TURSO, which is already False in any environment without
    TURSO_DATABASE_URL set.
    """
    monkeypatch.setattr(config, "SQLITE_PATH", tmp_path / "test_tickets.db")
    db.init_db()
    yield db
```

Install: `pip install -r requirements-dev.txt`

- [ ] **Step 2: Write the failing test**

Create `tests/test_db_acknowledged_at.py`:

```python
from app import db


def test_acknowledged_at_round_trips(isolated_db):
    ticket_id = db.create_ticket("Some issue text", created_at=1000.0)

    db.update_ticket(ticket_id, acknowledged_at=1234.5)

    ticket = db.get_ticket(ticket_id)
    assert ticket["acknowledged_at"] == 1234.5


def test_acknowledged_at_defaults_to_none(isolated_db):
    ticket_id = db.create_ticket("Some other issue text", created_at=1000.0)

    ticket = db.get_ticket(ticket_id)
    assert ticket["acknowledged_at"] is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_db_acknowledged_at.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such column: acknowledged_at` (or a `KeyError` from `_row_to_dict`), since the column doesn't exist yet.

- [ ] **Step 4: Add the column**

In `app/db.py`, the `SCHEMA` string currently ends the `tickets` table with (around line 44-45):

```python
    resolution_evidence TEXT,          -- one-sentence AI critique quote, shown in the Insights tab
    resolution_scored_at REAL          -- unset means not graded yet (or not closed yet)
);
```

Change to:

```python
    resolution_evidence TEXT,          -- one-sentence AI critique quote, shown in the Insights tab
    resolution_scored_at REAL,         -- unset means not graded yet (or not closed yet)
    acknowledged_at REAL               -- set when a POC acknowledges a ticket in Zoho; nullable,
                                        -- unpopulated until a future write path exists - see
                                        -- docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md
);
```

And in `init_db()`'s ALTER-TABLE-if-missing loop (around line 143-144), currently:

```python
            for col in ("resolution_score", "resolution_ack", "resolution_investigation",
                        "resolution_root_cause", "resolution_sla", "resolution_detail", "resolution_scored_at"):
```

Change to:

```python
            for col in ("resolution_score", "resolution_ack", "resolution_investigation",
                        "resolution_root_cause", "resolution_sla", "resolution_detail", "resolution_scored_at",
                        "acknowledged_at"):
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_db_acknowledged_at.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add app/db.py pytest.ini conftest.py requirements-dev.txt tests/test_db_acknowledged_at.py
git commit -m "Add nullable acknowledged_at column and pytest test harness"
```

---

### Task 2: `app/poc_queue.py` — POC queue selection and ack/SLA state logic

**Files:**
- Modify: `app/config.py` (append new section at end of file)
- Create: `app/poc_queue.py`
- Create: `tests/test_poc_queue.py`

**Interfaces:**
- Consumes: `isolated_db` fixture (Task 1); `db.list_all_tickets()`, `db.create_ticket(...)`, `db.update_ticket(ticket_id, **fields)`; `taxonomy.get(category_id) -> dict | None` (returns a dict with `poc_primary`, `parent_id`, `parent_name` among other keys — see `app/taxonomy.py:57-60`); `quality_scorer.CLOSED_STATUSES` (a `set[str]`, `app/quality_scorer.py:31`).
- Produces (used by Task 3): `poc_queue.resolve_poc_email(token: str | None) -> str | None` and `poc_queue.build_poc_queue(poc_email: str, now: float | None = None) -> dict`, where the returned dict has shape `{"tickets": [ {...per-ticket fields, see below...} ], "summary": {"breached": int, "needs_ack_now": int, "on_track": int}}`. Per-ticket fields: `id`, `zoho_ticket_id`, `category_group_code`, `category_group_name`, `created_at`, `ack_deadline_at`, `acknowledged_at`, `ack_state` (`"acknowledged"|"pending"|"missed"`), `ack_urgent` (bool), `sla_hours`, `sla_deadline_at`, `sla_state` (`"on_track"|"at_risk"|"breached"`), `sla_overdue_seconds` (present only when breached), `priority` (present only when Zoho sent one).

- [ ] **Step 1: Add config for tokens, ack/SLA windows, and category SLA hours**

Append to the end of `app/config.py` (after the existing `ZOHO_WEBHOOK_SECRET = _env("ZOHO_WEBHOOK_SECRET")` line):

```python
# --- POC ticket-queue extension (read-only queue popup) -------------------
# One small Chrome extension per POC (see docs/superpowers/specs/
# 2026-09-09-poc-ticket-queue-extension-design.md) authenticates with a
# single per-POC bearer token instead of the session-cookie login the main
# UI uses - matches ADMIN_USERNAME/PASSWORD and ZOHO_WEBHOOK_SECRET's
# existing "secret lives in one env var" pattern. Add one more _env() line
# here (and a matching env var) for each future POC extension.
POC_TOKENS: dict[str, str] = {
    "ranjith.kumar@nxtwave.co.in": _env("POC_TOKEN_RANJITH_KUMAR"),
}

# How long a POC has to acknowledge a new ticket before it counts as missed.
ACK_WINDOW_HOURS = 4.0
# Inside the last this-many minutes of an still-open ack window, the UI
# flags it "urgent" rather than just "due".
ACK_URGENT_MINUTES = 15.0
# Inside the last this fraction of the SLA window, the UI flags "at risk"
# rather than "on track".
SLA_AT_RISK_FRACTION = 0.15

# SLA hours per top-level taxonomy group code. Zoho never sends its own SLA
# deadline (only a categorical sla_breach_status string), so this table is
# the only source of the SLA countdown shown in a POC queue extension. Only
# G01 is populated today since it's the only group
# ranjith.kumar@nxtwave.co.in (the only POC extension built so far) is
# routed to - add more rows here as more POC extensions are built for other
# taxonomy groups.
CATEGORY_SLA_HOURS: dict[str, float] = {
    "G01": 24.0,
}
CATEGORY_SLA_HOURS_DEFAULT = 48.0
```

Also add to `.env.example`, right after the existing `ZOHO_WEBHOOK_SECRET=` line and its comment block:

```
# Per-POC token for the read-only ticket-queue Chrome extension - sent as
# the X-POC-Token header. One line per POC extension. Generate with:
# python -c "import secrets; print(secrets.token_hex(32))"
POC_TOKEN_RANJITH_KUMAR=
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_poc_queue.py`:

```python
import pytest

from app import config, db, poc_queue

POC_EMAIL = "ranjith.kumar@nxtwave.co.in"


def _make_ticket(created_at, category_id="G01-S01", status="classified",
                  raw_payload=None, acknowledged_at=None, zoho_ticket_id=None):
    ticket_id = db.create_ticket(
        "some issue text", zoho_ticket_id=zoho_ticket_id, raw_payload=raw_payload, created_at=created_at,
    )
    db.update_ticket(ticket_id, status=status, category_id=category_id, acknowledged_at=acknowledged_at)
    return ticket_id


def test_ticket_routed_to_this_poc_is_included(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert len(result["tickets"]) == 1
    assert result["tickets"][0]["category_group_code"] == "G01"
    assert result["tickets"][0]["category_group_name"] == "QA Report / Instructor Evaluation"


def test_ticket_routed_to_a_different_poc_is_excluded(isolated_db):
    now = 1_000_000.0
    # G04-S01 routes to narayana.dubbala/gompa.mounica, not ranjith
    _make_ticket(created_at=now - 3600, category_id="G04-S01")

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["tickets"] == []


def test_comma_separated_poc_primary_matches_by_containment(isolated_db):
    now = 1_000_000.0
    # G03-S01's poc_primary is "catherine..., gauthami..., ankon..." (comma list)
    _make_ticket(created_at=now - 3600, category_id="G03-S01")

    result = poc_queue.build_poc_queue("catherine.joannamathews@nxtwave.co.in", now=now)

    assert len(result["tickets"]) == 1


def test_needs_human_review_ticket_is_excluded_regardless_of_category(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, status="needs_human_review")

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["tickets"] == []


def test_closed_zoho_ticket_is_excluded(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, raw_payload={"ticket_status": "Resolved By POC"})

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["tickets"] == []


def test_ticket_with_no_ticket_status_is_treated_as_open(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, raw_payload={"some_other_field": "x"})

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert len(result["tickets"]) == 1


def test_ack_state_pending_when_within_window(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)  # 1h old, 4h ack window

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "pending"
    assert ticket["ack_urgent"] is False


def test_ack_state_urgent_in_last_15_minutes(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - (4 * 3600 - 10 * 60))  # 10 min left of the 4h window

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "pending"
    assert ticket["ack_urgent"] is True


def test_ack_state_missed_after_window_passes(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - (4 * 3600 + 60))  # 1 minute past the 4h window

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "missed"


def test_ack_state_acknowledged_when_timestamp_set(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - (4 * 3600 + 60), acknowledged_at=now - 3600)

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["ack_state"] == "acknowledged"
    assert ticket["acknowledged_at"] == now - 3600


def test_sla_on_track_well_within_window(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)  # 1h of 24h - nowhere near the last 15%

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["sla_hours"] == 24.0
    assert ticket["sla_state"] == "on_track"


def test_sla_at_risk_in_last_15_percent_of_window(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    # 24h window, 15% = 3.6h; 2h remaining is inside that
    _make_ticket(created_at=now - (24 * 3600 - 2 * 3600))

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["sla_state"] == "at_risk"


def test_sla_breached_past_deadline_reports_overdue_seconds(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    _make_ticket(created_at=now - (24 * 3600 + 3600))  # 1h past the 24h deadline

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["sla_state"] == "breached"
    assert ticket["sla_overdue_seconds"] == pytest.approx(3600.0)


def test_unmapped_group_falls_back_to_default_sla_hours(isolated_db, monkeypatch):
    monkeypatch.setattr(config, "CATEGORY_SLA_HOURS_DEFAULT", 48.0)
    now = 1_000_000.0
    # G09-S04 "Skip Level Escalation (General)" isn't in CATEGORY_SLA_HOURS
    ticket_id = db.create_ticket("some issue text", created_at=now - 3600)
    db.update_ticket(ticket_id, status="classified", category_id="G09-S04")

    result = poc_queue.build_poc_queue("prudhviraj.garlapati@nxtwave.co.in", now=now)

    assert result["tickets"][0]["sla_hours"] == 48.0


def test_priority_passed_through_from_raw_payload_when_present(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600, raw_payload={"priority_level": "P1"})

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert ticket["priority"] == "P1"


def test_priority_omitted_when_zoho_sends_none(isolated_db):
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)

    ticket = poc_queue.build_poc_queue(POC_EMAIL, now=now)["tickets"][0]

    assert "priority" not in ticket


def test_summary_counts_breached_needs_ack_now_and_on_track(isolated_db, monkeypatch):
    monkeypatch.setitem(config.CATEGORY_SLA_HOURS, "G01", 24.0)
    now = 1_000_000.0
    _make_ticket(created_at=now - 3600)                # on track, ack pending, not urgent
    _make_ticket(created_at=now - (24 * 3600 + 3600))  # SLA breached, ack missed too

    result = poc_queue.build_poc_queue(POC_EMAIL, now=now)

    assert result["summary"] == {"breached": 1, "needs_ack_now": 1, "on_track": 1}


def test_resolve_poc_email_with_valid_token(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")

    assert poc_queue.resolve_poc_email("secret-token") == POC_EMAIL


def test_resolve_poc_email_with_unknown_or_missing_token(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")

    assert poc_queue.resolve_poc_email("wrong-token") is None
    assert poc_queue.resolve_poc_email(None) is None
    assert poc_queue.resolve_poc_email("") is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_poc_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.poc_queue'`

- [ ] **Step 4: Implement `app/poc_queue.py`**

Create `app/poc_queue.py`:

```python
"""
Read-only ticket queue for a single POC (Point of Contact) - backs the
GET /api/extension/my-tickets endpoint consumed by that POC's Chrome
extension. See docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md
for the full design and the reasoning behind every decision below.

Deliberately has no write path: nothing here ever calls db.update_ticket.
"""
import secrets
import time

from . import config, db
from .quality_scorer import CLOSED_STATUSES
from .taxonomy import taxonomy

ACK_ACKNOWLEDGED = "acknowledged"
ACK_PENDING = "pending"
ACK_MISSED = "missed"

SLA_ON_TRACK = "on_track"
SLA_AT_RISK = "at_risk"
SLA_BREACHED = "breached"

OPEN_STATUSES = ("classified", "corrected")


def resolve_poc_email(token: str | None) -> str | None:
    """
    Reverse-lookup config.POC_TOKENS: the presented X-POC-Token header value
    -> the POC email it authenticates as, or None if it's missing, empty, or
    doesn't match any configured token. Constant-time comparison per token
    (like require_webhook_secret), not a plain dict lookup by value, since
    token values are secrets.
    """
    if not token:
        return None
    for email, configured_token in config.POC_TOKENS.items():
        if configured_token and secrets.compare_digest(token, configured_token):
            return email
    return None


def _poc_matches(poc_primary_field: str | None, poc_email: str) -> bool:
    """
    True if poc_email is one of poc_primary_field's comma-separated entries
    (case-insensitive, whitespace-trimmed). taxonomy.json's poc_primary is
    free text - sometimes one clean email, sometimes several comma-separated,
    sometimes a prose placeholder with no email at all ("Respective
    Capability Manager") - so this is containment-in-a-list, never exact
    string equality.
    """
    if not poc_primary_field:
        return False
    entries = {e.strip().casefold() for e in poc_primary_field.split(",")}
    return poc_email.strip().casefold() in entries


def _is_closed(raw_payload: dict | None) -> bool:
    """
    True only when raw_payload carries a ticket_status Zoho considers closed
    (the same CLOSED_STATUSES set quality_scorer.py already uses for
    resolution grading - one definition of "closed", not two). A ticket with
    no ticket_status at all (never came from Zoho, or Zoho hasn't sent a
    status yet) is always treated as open.
    """
    if not raw_payload:
        return False
    status = raw_payload.get("ticket_status")
    return bool(status) and status in CLOSED_STATUSES


def _compute_ack(created_at: float, acknowledged_at: float | None, now: float) -> dict:
    deadline = created_at + config.ACK_WINDOW_HOURS * 3600
    if acknowledged_at is not None:
        return {
            "ack_deadline_at": deadline,
            "acknowledged_at": acknowledged_at,
            "ack_state": ACK_ACKNOWLEDGED,
            "ack_urgent": False,
        }
    remaining = deadline - now
    if remaining <= 0:
        return {
            "ack_deadline_at": deadline,
            "acknowledged_at": None,
            "ack_state": ACK_MISSED,
            "ack_urgent": False,
        }
    return {
        "ack_deadline_at": deadline,
        "acknowledged_at": None,
        "ack_state": ACK_PENDING,
        "ack_urgent": remaining <= config.ACK_URGENT_MINUTES * 60,
    }


def _sla_hours_for_group(group_code: str) -> float:
    return config.CATEGORY_SLA_HOURS.get(group_code, config.CATEGORY_SLA_HOURS_DEFAULT)


def _compute_sla(created_at: float, group_code: str, now: float) -> dict:
    hours = _sla_hours_for_group(group_code)
    window_seconds = hours * 3600
    deadline = created_at + window_seconds
    remaining = deadline - now

    out = {"sla_hours": hours, "sla_deadline_at": deadline}
    if remaining <= 0:
        out["sla_state"] = SLA_BREACHED
        out["sla_overdue_seconds"] = -remaining
    elif remaining <= window_seconds * config.SLA_AT_RISK_FRACTION:
        out["sla_state"] = SLA_AT_RISK
    else:
        out["sla_state"] = SLA_ON_TRACK
    return out


def build_poc_queue(poc_email: str, now: float | None = None) -> dict:
    """
    Every open ticket routed to poc_email, with ack/SLA state resolved as of
    `now` (defaults to the real current time - overridable so tests can pin
    a fixed clock instead of racing real time). See module docstring and the
    design doc for the selection rules.
    """
    if now is None:
        now = time.time()

    tickets_out = []
    for t in db.list_all_tickets():
        if t["status"] not in OPEN_STATUSES:
            continue
        category_id = t.get("category_id")
        if not category_id:
            continue
        leaf = taxonomy.get(category_id)
        if not leaf:
            continue
        if not _poc_matches(leaf.get("poc_primary"), poc_email):
            continue
        if _is_closed(t.get("raw_payload")):
            continue

        raw_payload = t.get("raw_payload") or {}
        entry = {
            "id": t["id"],
            "zoho_ticket_id": t.get("zoho_ticket_id"),
            "category_group_code": leaf["parent_id"],
            "category_group_name": leaf["parent_name"],
            "created_at": t["created_at"],
            **_compute_ack(t["created_at"], t.get("acknowledged_at"), now),
            **_compute_sla(t["created_at"], leaf["parent_id"], now),
        }
        priority = raw_payload.get("priority_level")
        if priority:
            entry["priority"] = priority
        tickets_out.append(entry)

    summary = {
        "breached": sum(1 for x in tickets_out if x["sla_state"] == SLA_BREACHED),
        "needs_ack_now": sum(
            1 for x in tickets_out if x["ack_state"] == ACK_MISSED or x["ack_urgent"]
        ),
        "on_track": sum(1 for x in tickets_out if x["sla_state"] == SLA_ON_TRACK),
    }
    return {"tickets": tickets_out, "summary": summary}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_poc_queue.py -v`
Expected: PASS (18 passed)

- [ ] **Step 6: Commit**

```bash
git add app/config.py .env.example app/poc_queue.py tests/test_poc_queue.py
git commit -m "Add poc_queue module: POC ticket selection and ack/SLA state computation"
```

---

### Task 3: `GET /api/extension/my-tickets` endpoint

**Files:**
- Modify: `app/main.py:17` (import line), `app/main.py:73-84` (add new dependency after `require_webhook_secret`)
- Create: `tests/test_extension_endpoint.py`

**Interfaces:**
- Consumes: `poc_queue.resolve_poc_email(token) -> str | None`, `poc_queue.build_poc_queue(poc_email, now=None) -> dict` (Task 2).
- Produces: `GET /api/extension/my-tickets` — requires header `X-POC-Token`; 401 if missing/invalid; 200 with the JSON body `poc_queue.build_poc_queue()` returns otherwise. Consumed by the Chrome extension in Task 4.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extension_endpoint.py`:

```python
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

POC_EMAIL = "ranjith.kumar@nxtwave.co.in"


def test_missing_token_returns_401(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")
    client = TestClient(app)

    response = client.get("/api/extension/my-tickets")

    assert response.status_code == 401


def test_wrong_token_returns_401(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")
    client = TestClient(app)

    response = client.get("/api/extension/my-tickets", headers={"X-POC-Token": "wrong"})

    assert response.status_code == 401


def test_valid_token_returns_this_pocs_queue(isolated_db, monkeypatch):
    monkeypatch.setitem(config.POC_TOKENS, POC_EMAIL, "secret-token")
    ticket_id = db.create_ticket("some issue text", created_at=1_000_000.0 - 3600)
    db.update_ticket(ticket_id, status="classified", category_id="G01-S01")
    client = TestClient(app)

    response = client.get("/api/extension/my-tickets", headers={"X-POC-Token": "secret-token"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["tickets"]) == 1
    assert body["tickets"][0]["category_group_code"] == "G01"
    assert "summary" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_extension_endpoint.py -v`
Expected: FAIL — 404 (route doesn't exist yet)

- [ ] **Step 3: Wire up the endpoint in `app/main.py`**

Change the import line (currently line 17):

```python
from . import auth, config, db, memory, classifier, quality_scorer
```

to:

```python
from . import auth, config, db, memory, classifier, quality_scorer, poc_queue
```

Immediately after the existing `require_webhook_secret` function (ends around line 84, right before `@app.on_event("startup")`), add:

```python
def require_poc_token(x_poc_token: str | None = Header(default=None, alias="X-POC-Token")) -> str:
    """
    Authenticates a single POC's Chrome extension (see
    docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md) -
    a per-POC bearer token, not the session-cookie login the main UI uses,
    since the extension has no login flow of its own. 401s on a missing or
    unrecognized token.
    """
    email = poc_queue.resolve_poc_email(x_poc_token)
    if not email:
        raise HTTPException(401, "Missing or invalid X-POC-Token header.")
    return email


@app.get("/api/extension/my-tickets")
def get_my_tickets(poc_email: str = Depends(require_poc_token)):
    """Read-only ticket queue for one POC's Chrome extension. Never writes anything."""
    return poc_queue.build_poc_queue(poc_email)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_extension_endpoint.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: all tests across Tasks 1-3 pass (no regressions).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_extension_endpoint.py
git commit -m "Add GET /api/extension/my-tickets endpoint with per-POC token auth"
```

---

### Task 4: Chrome extension popup

**Files:**
- Create: `extensions/poc-ranjith-kumar/manifest.json`
- Create: `extensions/poc-ranjith-kumar/config.js`
- Create: `extensions/poc-ranjith-kumar/popup.html`
- Create: `extensions/poc-ranjith-kumar/popup.css`
- Create: `extensions/poc-ranjith-kumar/popup.js`
- Create: `extensions/poc-ranjith-kumar/README.md`

**Interfaces:**
- Consumes: `GET /api/extension/my-tickets` (Task 3) — JSON body `{"tickets": [ {id, zoho_ticket_id, category_group_code, category_group_name, created_at, ack_deadline_at, acknowledged_at, ack_state, ack_urgent, sla_hours, sla_deadline_at, sla_state, sla_overdue_seconds?, priority?} ], "summary": {breached, needs_ack_now, on_track}}`.
- Produces: a loadable Manifest V3 extension folder — no other code depends on this task's output.

- [ ] **Step 1: Create the manifest**

Create `extensions/poc-ranjith-kumar/manifest.json`:

```json
{
  "manifest_version": 3,
  "name": "Ticket Queue — Ranjith Kumar",
  "version": "1.0.0",
  "description": "Read-only queue of QA Report tickets awaiting acknowledgement or approaching SLA, for ranjith.kumar@nxtwave.co.in.",
  "action": {
    "default_popup": "popup.html"
  },
  "permissions": [],
  "host_permissions": [
    "http://localhost:8000/*",
    "https://zoho-tickets.onrender.com/*"
  ]
}
```

- [ ] **Step 2: Create the per-POC config**

Create `extensions/poc-ranjith-kumar/config.js`:

```javascript
// One extension = one POC = one baked-in token. Rotate by regenerating the
// token value in the server's .env (POC_TOKEN_RANJITH_KUMAR) and pasting the
// same value here, then reloading the unpacked extension in chrome://extensions.
const POC_CONFIG = {
  apiBaseUrl: "http://localhost:8000",
  token: "REPLACE_WITH_THE_SAME_VALUE_AS_POC_TOKEN_RANJITH_KUMAR_IN_THE_SERVER_ENV",
};
```

- [ ] **Step 3: Create the popup markup**

Create `extensions/poc-ranjith-kumar/popup.html`:

```html
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="popup.css">
</head>
<body>
  <header>
    <h1>My Ticket Queue</h1>
    <p class="poc-email">ranjith.kumar@nxtwave.co.in</p>
  </header>

  <div id="summary" class="summary-row"></div>

  <div class="sort-tabs" id="sortTabs">
    <button class="sort-tab active" data-sort="risk">Risk</button>
    <button class="sort-tab" data-sort="priority">Priority</button>
    <button class="sort-tab" data-sort="sla">SLA left</button>
  </div>

  <div id="list" class="ticket-list"><div class="empty-state">Loading&hellip;</div></div>

  <script src="config.js"></script>
  <script src="popup.js"></script>
</body>
</html>
```

- [ ] **Step 4: Create the stylesheet**

Create `extensions/poc-ranjith-kumar/popup.css`:

```css
:root {
  --bad: #c0392b;
  --bad-bg: #fdecea;
  --warn: #8a6300;
  --warn-bg: #fff8e1;
  --ok: #12805c;
  --ok-bg: #e6f6ef;
  --text: #1a1a1a;
  --muted: #666;
  --border: #e2e2e2;
}

* { box-sizing: border-box; }

body {
  width: 340px;
  margin: 0;
  padding: 12px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--text);
  background: #fff;
}

header h1 {
  font-size: 15px;
  margin: 0 0 2px;
}

.poc-email {
  font-size: 11px;
  color: var(--muted);
  margin: 0 0 10px;
}

.summary-row {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.summary-stat {
  flex: 1;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 8px;
  text-align: center;
}

.summary-stat .count {
  display: block;
  font-size: 18px;
  font-weight: 700;
}

.summary-stat .label {
  display: block;
  font-size: 10px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.summary-stat.breached .count { color: var(--bad); }
.summary-stat.needs-ack .count { color: var(--warn); }
.summary-stat.on-track .count { color: var(--ok); }

.sort-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 10px;
}

.sort-tab {
  flex: 1;
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 6px;
  padding: 5px 0;
  font-size: 11px;
  cursor: pointer;
}

.sort-tab.active {
  background: var(--text);
  color: #fff;
  border-color: var(--text);
}

.ticket-list {
  max-height: 420px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ticket-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
}

.ticket-card-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.ticket-id {
  font-family: "SFMono-Regular", Consolas, monospace;
  font-weight: 700;
  font-size: 13px;
}

.priority-pill {
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
}

.ticket-category {
  font-size: 11px;
  color: var(--muted);
  margin: 3px 0 6px;
}

.pill {
  display: inline-block;
  font-size: 11px;
  border-radius: 5px;
  padding: 3px 6px;
  margin: 2px 4px 0 0;
}

.pill.bad { background: var(--bad-bg); color: var(--bad); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.ok { background: var(--ok-bg); color: var(--ok); }
.pill.neutral { background: #f2f2f2; color: var(--muted); }

.empty-state {
  text-align: center;
  color: var(--muted);
  font-size: 12px;
  padding: 24px 8px;
}

.empty-state.error {
  color: var(--bad);
}
```

- [ ] **Step 5: Create the popup logic**

Create `extensions/poc-ranjith-kumar/popup.js`:

```javascript
const state = { tickets: [], summary: null, sort: "risk", error: null };

const PRIORITY_ORDER = { P1: 0, P2: 1, P3: 2, P4: 3 };

function formatDuration(totalSeconds) {
  const abs = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(abs / 3600);
  const minutes = Math.floor((abs % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function sortTickets(tickets, mode, now) {
  const copy = tickets.slice();
  if (mode === "priority") {
    copy.sort((a, b) => (PRIORITY_ORDER[a.priority] ?? 99) - (PRIORITY_ORDER[b.priority] ?? 99));
    return copy;
  }
  if (mode === "sla") {
    copy.sort((a, b) => (a.sla_deadline_at - now) - (b.sla_deadline_at - now));
    return copy;
  }
  // "risk" (default): breached first, then ack missed, then ack urgent,
  // then the rest ordered by soonest-to-breach.
  const rank = (t) => {
    if (t.sla_state === "breached") return 0;
    if (t.ack_state === "missed") return 1;
    if (t.ack_urgent) return 2;
    return 3;
  };
  copy.sort((a, b) => {
    const diff = rank(a) - rank(b);
    if (diff !== 0) return diff;
    return (a.sla_deadline_at - now) - (b.sla_deadline_at - now);
  });
  return copy;
}

function ackLine(ticket, now) {
  if (ticket.ack_state === "acknowledged") {
    return {
      text: `Acknowledged · ${formatDuration(ticket.acknowledged_at - ticket.created_at)} after raise`,
      cls: "ok",
    };
  }
  if (ticket.ack_state === "missed") {
    return {
      text: `Ack window missed · ${formatDuration(now - ticket.ack_deadline_at)} overdue`,
      cls: "bad",
    };
  }
  return {
    text: `Ack due in ${formatDuration(ticket.ack_deadline_at - now)}`,
    cls: ticket.ack_urgent ? "warn" : "neutral",
  };
}

function slaLine(ticket, now) {
  if (ticket.sla_state === "breached") {
    return { text: `SLA breached · ${formatDuration(ticket.sla_overdue_seconds)} overdue`, cls: "bad" };
  }
  if (ticket.sla_state === "at_risk") {
    return { text: `SLA at risk · ${formatDuration(ticket.sla_deadline_at - now)} left`, cls: "warn" };
  }
  return { text: `On track · ${formatDuration(ticket.sla_deadline_at - now)} left`, cls: "ok" };
}

function renderSummary(summary) {
  document.getElementById("summary").innerHTML = `
    <div class="summary-stat breached"><span class="count">${summary.breached}</span><span class="label">Out of SLA</span></div>
    <div class="summary-stat needs-ack"><span class="count">${summary.needs_ack_now}</span><span class="label">Needs ack now</span></div>
    <div class="summary-stat on-track"><span class="count">${summary.on_track}</span><span class="label">On track</span></div>
  `;
}

function renderTicketCard(ticket, now) {
  const ack = ackLine(ticket, now);
  const sla = slaLine(ticket, now);
  const priorityHtml = ticket.priority ? `<span class="priority-pill">${ticket.priority}</span>` : "";
  return `
    <div class="ticket-card">
      <div class="ticket-card-top">
        <span class="ticket-id">${ticket.zoho_ticket_id || ticket.id}</span>
        ${priorityHtml}
      </div>
      <div class="ticket-category">${ticket.category_group_code} · ${ticket.category_group_name}</div>
      <div class="pill ${ack.cls}">${ack.text}</div>
      <div class="pill ${sla.cls}">${sla.text}</div>
    </div>
  `;
}

function render() {
  const now = Date.now() / 1000;
  const listEl = document.getElementById("list");

  if (state.error) {
    document.getElementById("summary").innerHTML = "";
    listEl.innerHTML = `<div class="empty-state error">${state.error}</div>`;
    return;
  }

  renderSummary(state.summary);

  if (state.tickets.length === 0) {
    listEl.innerHTML = `<div class="empty-state">Queue is empty — nothing open right now.</div>`;
    return;
  }

  const sorted = sortTickets(state.tickets, state.sort, now);
  listEl.innerHTML = sorted.map((t) => renderTicketCard(t, now)).join("");
}

function setupTabs() {
  document.querySelectorAll(".sort-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".sort-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.sort = btn.dataset.sort;
      render();
    });
  });
}

async function load() {
  if (!POC_CONFIG.token || POC_CONFIG.token.startsWith("REPLACE_WITH")) {
    state.error = "This extension isn't configured yet — set a real token in config.js.";
    render();
    return;
  }
  try {
    const response = await fetch(`${POC_CONFIG.apiBaseUrl}/api/extension/my-tickets`, {
      headers: { "X-POC-Token": POC_CONFIG.token },
    });
    if (!response.ok) {
      state.error = `Server returned ${response.status}. Check the token and server URL in config.js.`;
      render();
      return;
    }
    const data = await response.json();
    state.tickets = data.tickets;
    state.summary = data.summary;
    state.error = null;
    render();
  } catch (err) {
    state.error = "Couldn't reach the server. Is it running, and is the URL in config.js correct?";
    render();
  }
}

setupTabs();
load();
```

- [ ] **Step 6: Create the setup README**

Create `extensions/poc-ranjith-kumar/README.md`:

```markdown
# Ticket Queue extension — ranjith.kumar@nxtwave.co.in

Read-only popup showing this POC's open QA Report tickets. See
`docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md`
for the full design.

## One-time setup

1. Generate a token: `python -c "import secrets; print(secrets.token_hex(32))"`
2. Add it to the server's `.env`: `POC_TOKEN_RANJITH_KUMAR=<the token>`
3. Restart the server so the new env var takes effect.
4. Paste the same token into this folder's `config.js` (`token` field).
5. Set `config.js`'s `apiBaseUrl` to wherever the server runs
   (`http://localhost:8000` for local dev, or the deployed URL).

## Load it in Chrome

1. Go to `chrome://extensions`, enable "Developer mode" (top right).
2. Click "Load unpacked" and select this folder (`extensions/poc-ranjith-kumar/`).
3. Click the extension's icon to open the popup.

## Known limitations (Step 1)

- Acknowledgement is never populated yet - every ticket shows as
  "not yet acknowledged" even if it was acknowledged in Zoho (design doc §2).
- No "Open in Zoho" link yet - no safe, browser-viewable Zoho record URL
  exists (design doc §2).
```

- [ ] **Step 7: Manually verify end-to-end**

Start the backend locally (adjust to however this repo normally runs it, e.g. `uvicorn app.main:app --reload`), then set a real token:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# put the printed value in .env as POC_TOKEN_RANJITH_KUMAR=<value>
# restart the server
```

Seed a couple of tickets in different states via a quick REPL so the popup has something to show:

```python
from app import db
db.init_db()
now = __import__("time").time()

# On track
t1 = db.create_ticket("QA report score seems wrong", created_at=now - 3600)
db.update_ticket(t1, status="classified", category_id="G01-S01", zoho_ticket_id="Z-1001")

# SLA breached, ack missed
t2 = db.create_ticket("QA report has no feedback at all", created_at=now - 26 * 3600,
                       raw_payload={"priority_level": "P1"})
db.update_ticket(t2, status="classified", category_id="G01-S02", zoho_ticket_id="Z-1002")
```

Paste the same token into `config.js`, load the extension unpacked (Step 6 above), open the popup, and confirm:
- Summary row shows 1 breached, 1 needs-ack-now, 0-or-1 on-track (matching the two seeded tickets).
- Ticket Z-1002 shows a red "SLA breached" pill and a red "Ack window missed" pill; Z-1001 shows teal/amber pills appropriate to its age.
- Clicking "Priority" and "SLA left" tabs reorders the two cards instantly, no network tab activity in DevTools.
- Stopping the backend and reopening the popup shows the "Couldn't reach the server" error state, not a blank popup.

- [ ] **Step 8: Commit**

```bash
git add extensions/poc-ranjith-kumar/
git commit -m "Add read-only ticket queue Chrome extension for ranjith.kumar@nxtwave.co.in"
```
