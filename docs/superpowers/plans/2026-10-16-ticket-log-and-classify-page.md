# Ticket Timeline Log + Classify Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Log with a per-ticket timeline of Zoho calls and errors (threshold table kept at the bottom), and add a Classify page where a pasted issue gets a suggestion, and a correction becomes an example plus an optional, previewed definition change.

**Architecture:** The live taxonomy moves from `app/taxonomy.json` into the database, so edits survive deploys. The webhook and the auto-classify loop write one-line events to a new `ticket_events` table, with the sentences built in a new `app/ticket_events.py`. The Classify page is a set of `/api/classify/*` endpoints. Definition drafts come from `app/definition_drafter.py`, and previews run the classifier against an in-memory copy of the taxonomy.

**Tech Stack:** Python 3 / FastAPI, SQLite locally and Turso (libSQL) in production through `app/db.py`, Chroma (`app/memory.py`), OpenRouter via the `openai` SDK, a vanilla-JS shell in `app/static/index.html`, and pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-ticket-log-and-classify-page-design.md`

**Prerequisite:** the uncommitted "two pages only" work (Log and Taxonomy, extension and grading removed) must be committed first, so this plan starts from a clean tree.

## Global Constraints

- **Login:** every page and `/api/*` route needs it. The only exception is `POST /api/webhooks/zoho/tickets`, which uses `X-Webhook-Secret`.
- **Password:** saving a correction, drafting, previewing, applying and undoing all need the Taxonomy password: the `X-Taxonomy-Password` header, checked with the existing `_require_taxonomy_password`. Getting a suggestion (`POST /api/classify`) needs only login.
- **SQL:** plain SQL that runs on both SQLite and Turso. New tables go in `db.SCHEMA` as `CREATE TABLE IF NOT EXISTS`. No `;` inside SQL comments, because `init_db` splits the schema on `;`.
- **Events never break Zoho:** recording an event must never raise into the webhook. Zoho must always get its category back.
- **Log lines:** one line per call, in plain English, with error details only on failures. No raw request or response bodies.
- **Override sentence:** must say why, in words, using `config.REPORTER_OVERRIDE_MIN_CONFIDENCE` (currently 0.9) shown as a percentage.
- **Checks only report.** They never block, apply or roll back anything by themselves.
- **Check size:** a check re-runs up to **5** existing examples from each affected subcategory.
- **Header order:** Log, Classify, Taxonomy. Log stays the default page. The new page's URL is `/classify`.
- **Old tickets:** tickets from before launch still appear in the Log, marked "no timeline (before this was recorded)".
- **Upload history:** CSV-uploaded history (`_is_upload_history`) stays out of the Log, as today.

## Review Focus

1. **The database fails while recording an event.** The webhook must still return the category. *Test:* Task 3, `test_event_failure_never_breaks_webhook`.
2. **A huge pasted issue (45,000 characters).** The Classify page still works, and the Log's first line is cut to 140 characters. *Tests:* Task 4, `test_first_line_is_trimmed`; Task 5, `test_classify_long_text`.
3. **A correction to the subcategory the model already picked, or to an unknown id.** It must be a 400, not a silent no-op or a self-contradicting definition. *Tests:* Task 5, `test_correct_rejects_unknown_category`; Task 6, `test_draft_rejects_same_category`.
4. **Undo after the same description was edited again on the Taxonomy page.** Return 409 and leave the newer text alone. *Test:* Task 7, `test_undo_refuses_when_changed_since`.
5. **A deploy ships a `taxonomy.json` with a higher version than the live copy.** The file wins. Equal or lower means the database wins. *Test:* Task 1, `test_startup_prefers_newer_file`.

---

### Task 1: Live taxonomy stored in the database

**Files:**
- Modify: `app/db.py`
  - Add a `taxonomy_store` table to `SCHEMA`.
  - Add `get_taxonomy_doc` and `put_taxonomy_doc`.
- Modify: `app/taxonomy.py`
  - Add `from_data`.
  - `save` writes to the database and bumps the version.
  - Add `load_live`.
- Modify: `app/main.py`: `startup()` calls `taxonomy.load_live()` right after `db.init_db()` and before the seed sync.
- Modify: `conftest.py`: a new autouse fixture snapshots and restores the global `taxonomy` state around each test.
- Test: `tests/test_taxonomy_store.py`

**Interfaces:**
- Produces:
  - `db.get_taxonomy_doc() -> dict | None`
  - `db.put_taxonomy_doc(data: dict) -> None`
  - `Taxonomy.from_data(data: dict) -> Taxonomy`: a standalone copy that never touches disk or the database. Task 6 uses it.
  - `Taxonomy.load_live() -> str`: returns `"db"` or `"file"`.
  - `Taxonomy.save(data: dict) -> None`: same validation as today, then sets `data["version"] = (self.version or 0) + 1`, writes `db.put_taxonomy_doc(data)` and reloads from `data`. It no longer writes `taxonomy.json`.
  - `Taxonomy.as_dict() -> dict`: a deep copy of `{version, categories}`. Tasks 5 to 7 edit this copy, then `save()` it.

- [ ] **Step 1: Write the failing tests** in `tests/test_taxonomy_store.py`:
  - `test_save_writes_db_not_file`: after `taxonomy.save(d)`, `db.get_taxonomy_doc()["categories"] == d["categories"]` and the mtime of `app/taxonomy.json` is unchanged.
  - `test_save_bumps_version`: saving at version `v` leaves `taxonomy.version == v + 1`.
  - `test_load_live_uses_db_copy`: put a doc with `version = file_version + 0` and an edited description, then `load_live() == "db"` and `taxonomy.get(id)["description"]` is the edited one.
  - `test_startup_prefers_newer_file`: a db doc with `version = file_version - 1`, then `load_live() == "file"` and the database now holds the file's version.
  - `test_load_live_seeds_empty_db`: with no doc, `load_live() == "file"` and `db.get_taxonomy_doc()` is not None.
  - `test_from_data_is_independent`: editing the result of `Taxonomy.from_data(taxonomy.as_dict())` leaves the global `taxonomy` unchanged.
- [ ] **Step 2: Run them and check they fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_taxonomy_store.py -q`. Expected: FAIL (no `get_taxonomy_doc`).
- [ ] **Step 3: Implement.**
  - Table: `taxonomy_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)`. A single row with key `'live'` holds the JSON.
  - `load_live` reads the file with the existing `reload()`. It then compares versions: if the database has none, or the file's version is higher, it writes the file data to the database and returns `"file"`. Otherwise it loads the database doc and returns `"db"`.
  - The conftest fixture saves `taxonomy._data` and `taxonomy._leaf_index` (deep copies) and restores them after each test.
- [ ] **Step 4: Run all tests and check they pass.** Run `.venv/Scripts/python.exe -m pytest -q`. Expected: every test passes, including `tests/test_taxonomy_memory_sync.py` unchanged.
- [ ] **Step 5: Add a line to the README "Taxonomy" section:** "The live taxonomy is stored in the database; `taxonomy.json` is only the starting copy. To ship a taxonomy change through git, set its `version` higher than the live one shown on the Taxonomy page."
- [ ] **Step 6: Commit.** `git commit -m "Store the live taxonomy in the database so edits survive deploys"`

### Task 2: Event storage and plain-English lines

**Files:**
- Modify: `app/db.py`: add the `ticket_events` table and its functions.
- Create: `app/ticket_events.py`
- Test: `tests/test_ticket_events.py`

**Interfaces:**
- Consumes: `taxonomy.get`, `ClassificationResult` (with `decision`, `model_category_id`, `model_confidence`, `category_id`, `confidence`).
- Produces (database):
  - `db.add_event(ticket_id: str, kind: str, summary: str, error: str | None = None, duration_ms: int | None = None) -> None`
  - `db.list_events(ticket_id: str) -> list[dict]`, oldest first. The keys are `kind`, `summary`, `error`, `duration_ms` and `created_at`.
  - `db.event_flags() -> dict[str, dict]`: maps `ticket_id` to `{"events": int, "errors": int}`, using one `GROUP BY` query.
  - `db.list_drafts() -> list[dict]`: drafts, newest first.
- Produces (`app/ticket_events.py`):
  - `KINDS = ("presubmit", "on_add", "edit", "transfer", "duplicate", "auto_classify", "error")`
  - `pick_text(leaf_id: str | None) -> str`: returns `"Group › Sub"`, or `"nothing"` when the id is None or unknown.
  - `override_sentence(result: ClassificationResult, instructor_leaf_id: str | None, threshold: float) -> str`
  - `presubmit_line(result: ClassificationResult | None, instructor_pick: str | None, sent_leaf_id: str, used_fallback: bool) -> str`
  - `on_add_line(adopted: bool, final_leaf_id: str | None) -> str`
  - `edit_line(old_status: str | None, new_status: str | None) -> str`
  - `transfer_line(from_leaf_id: str | None, to_leaf_id: str) -> str`
  - `duplicate_line(first_zoho_id: str | None) -> str`
  - `auto_classify_line(result: ClassificationResult) -> str`

- [ ] **Step 1: Write the failing tests.** Use `t = 0.9`, with A = Campus Network / Wi-Fi Issue (`G08-S02`) and B = Recording Issue (`G03-S04`).
  - `test_override_sentence_overrode`: `override_sentence(result, "G03-S04", 0.9)`, with decision `overrode`, model `G08-S02` at 0.9 and final `G08-S02`, gives exactly: `"Model picked Facilities & Equipment › Campus Network / Wi-Fi Issue (90%) over the instructor's Instructor Platform (Teach OS) › Recording Issue (Platform / Software): at or above the 90% bar, so it replaced the pick."`
  - `test_override_sentence_kept`: decision `kept`, model 0.72. The sentence contains `"(72%)"`, `"below the 90% bar"` and `"instructor's pick was kept"`.
  - `test_override_sentence_agreed_and_none`: `agreed` contains `"agreed with the instructor's pick"`. `none` contains `"No subcategory was picked by the instructor"`.
  - `test_presubmit_line_failure`: `presubmit_line(None, "Recording Issue", "G14-S01", True)` contains `"Classification failed"` and `"fallback"`.
  - `test_add_and_list_events`: add a `presubmit` event and then an `error` event, then `list_events` returns them in that order and `event_flags()[tid] == {"events": 2, "errors": 1}`. `errors` counts events of kind `error`.
- [ ] **Step 2: Run them and check they fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_ticket_events.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement.**
  - Table: `ticket_events (id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL, error TEXT, duration_ms INTEGER, created_at REAL NOT NULL)`, with `CREATE INDEX IF NOT EXISTS idx_events_ticket ON ticket_events(ticket_id, created_at)`.
  - Confidence is shown as `round(c * 100)` followed by `%`.
- [ ] **Step 4: Run the tests and check they pass.** Run `.venv/Scripts/python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** `git commit -m "Add ticket events table and plain-English event lines"`

### Task 3: Record events from the webhook and auto-classify

**Files:**
- Modify: `app/main.py`
  - `webhook_new_zoho_ticket`, `_store_presubmit_draft` (return the draft id), `_mark_if_duplicate`, `_classify_pending_batch`.
  - A new helper, `_event(...)`.
- Test: `tests/test_event_recording.py`

**Interfaces:**
- Consumes: Task 2's `db.add_event` and the `ticket_events.*_line` functions.
- Produces:
  - `_event(ticket_id: str | None, kind: str, summary: str, error: str | None = None, started: float | None = None) -> None`. It does nothing when `ticket_id` is None, and it catches and logs every exception. When `started` is set, `duration_ms = int((time.time() - started) * 1000)`.
  - `_store_presubmit_draft(...) -> str | None` (the draft id).

**What each path records:**
- **Pre-submit:** a `presubmit` event on the draft id, after the draft is stored. On a classify failure, the `presubmit` line plus an `error` event whose `error` is the exception text (cut to 1000 characters).
- **On Add:** an `on_add` event on the ticket id, with `adopted = adopted is not None`. If the duplicate check marked it, add a `duplicate` event. On a classify failure, an `error` event.
- **Existing ticket:** an `edit` event when `raw_payload.ticket_status` changed. When a transfer is detected, a `transfer` event as well. When `used_fallback`, an `error` event with summary `"Sent the fallback category"` and error set to the fallback reason.
- **Auto-classify loop:** an `auto_classify` event per classified ticket, and an `error` event per failure.

- [ ] **Step 1: Write the failing tests.** Reuse the `_model_says` and `_presubmit` patterns from `tests/test_routing_log.py`.
  - `test_presubmit_then_on_add_builds_one_timeline`: after a pre-submit and then an On Add with the same text, `db.list_events(ticket_id)` has kinds `["presubmit", "on_add"]`.
  - `test_classify_failure_records_error`: classify raises `RuntimeError("boom")`, and the timeline has an `error` event whose `error` contains `"boom"`.
  - `test_event_failure_never_breaks_webhook`: monkeypatch `db.add_event` to raise. Both webhook calls still return 200 with `sub_category_of_the_issue`.
  - `test_transfer_and_edit_recorded`: an edit with `ticket_status` changed adds `edit`. A subcategory change to another leaf adds `transfer`.
  - `test_auto_classify_records_event`: a pending ticket, then `_classify_pending_batch(10, "k")`, gives an `auto_classify` event.
- [ ] **Step 2: Run them and check they fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_event_recording.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement the wiring described above.**
- [ ] **Step 4: Run all tests and check they pass.** Run `.venv/Scripts/python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** `git commit -m "Record a timeline event for every Zoho call, edit, transfer and error"`

### Task 4: Log API and page

**Files:**
- Modify: `app/main.py`: add `GET /api/ticket-log` and `GET /api/ticket-log/{ticket_id}`. Keep `GET /api/routing-log` as is, because the threshold section uses its `summary`.
- Modify: `app/static/index.html`: rebuild `#logView`.
- Test: `tests/test_ticket_log_api.py`

**Interfaces:**
- Consumes: Task 2's `db.event_flags`, `db.list_events`, `db.list_drafts` and `ticket_events.pick_text`.
- Produces:
  - `GET /api/ticket-log?q=&date_from=&date_to=&errors_only=false&unsubmitted=false` returns `{"tickets": [...], "total": int}`. The list is newest first and capped at 300. Each item is `{id, zoho_ticket_id, created_at, first_line, final, has_error, has_timeline, submitted}`:
    - `first_line`: the first line of the issue, cut to 140 characters plus `"…"`.
    - `final`: `pick_text(category_id)`.
    - `q`: matches `zoho_ticket_id` as a case-insensitive substring.
    - `unsubmitted=true`: returns drafts instead (with `submitted` false).
  - `GET /api/ticket-log/{ticket_id}` returns `{"ticket": {same fields + issue_text}, "events": db.list_events(...)}`, or 404.

**Page** (copy and layout per the spec):
- At the top: a Ticket ID search, the date range, a "Has errors" checkbox and a "Not submitted" checkbox.
- A newest-first list. Each row shows the Ticket ID, time, first line and final category, a red marker when `has_error`, and "no timeline (before this was recorded)" when `has_timeline` is false.
- Clicking a row expands its timeline: one line each, showing time, the event kind label, the summary, and the duration when present. Error events are red, with the error text below.
- At the bottom, a small **Override threshold** section: today's table built from `/api/routing-log`'s `summary.simulation`, keeping the current-value bold and best-value green styling.
- Remove the old decision tiles and the per-ticket routing table.

- [ ] **Step 1: Write the failing tests.**
  - `test_list_and_timeline`
  - `test_search_by_zoho_id` (`q="E2E"` matches `"E2E-1"` only)
  - `test_errors_only`
  - `test_unsubmitted_lists_drafts`
  - `test_first_line_is_trimmed` (a 45,000-character issue gives `len(first_line) <= 141`)
  - `test_requires_login`
- [ ] **Step 2: Run them and check they fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_ticket_log_api.py -q`. Expected: FAIL (404s).
- [ ] **Step 3: Implement the endpoints, then the page.**
- [ ] **Step 4: Run all tests and check they pass.** Run `.venv/Scripts/python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Look at it.** Start the app locally with `ZOHO_WEBHOOK_SECRET=local-test-secret`, send the test tickets from the earlier session's `send_test_tickets.py` pattern, open `/log` and screenshot it. Expected: the list, an expanded timeline with an override sentence, and the threshold section at the bottom.
- [ ] **Step 6: Commit.** `git commit -m "Log page: per-ticket timeline with errors, threshold table kept at the bottom"`

### Task 5: Classify page: suggest and correct

**Files:**
- Modify: `app/main.py`
  - Add `/classify` to the `index()` routes and to `PAGE_PATHS`.
  - Add `POST /api/classify` and `POST /api/classify/correct`.
- Modify: `app/static/login.html`: add `/classify` to `pages`.
- Modify: `app/static/index.html`: add the nav link `navBtn-classify` (between Log and Taxonomy), `PAGES.classify = { path: '/classify', title: 'Classify' }` and `#classifyView`.
- Modify: `tests/test_pages.py`: include `/classify` and `navBtn-classify`.
- Test: `tests/test_classify_page.py`

**Interfaces:**
- Consumes: Task 1's `taxonomy.as_dict()` and `taxonomy.save()`; `classifier.classify`; `_sync_seed_examples`.
- Produces:
  - `POST /api/classify {text}` returns `{category_id, category, subcategory, confidence, reasoning}`. It's a 400 when the text is empty, and a 503 with a clear message when `OPENROUTER_API_KEY` is unset. It never creates a ticket or an event.
  - `POST /api/classify/correct {text, category_id}` (password) returns `{added: bool, recheck: {category_id, category, subcategory, confidence, correct: bool}}`.
    - It's a 400 for an unknown `category_id`.
    - The issue is appended to that subcategory's `examples`, unless the same text (compared after collapsing spaces and ignoring case) is already there, in which case `added` is false.
    - It then calls `taxonomy.save()`, then `_sync_seed_examples("classify correction")` synchronously, so memory is current before the re-check, then re-runs `classify(text)`.
  - `_label(leaf_id) -> dict`: returns `{category_id, category, subcategory}`. Task 6 reuses it.

**Page:**
- A textarea, then **Suggest**, then the result card (category › subcategory, confidence %, reasoning).
- **Wrong?** opens a category select and a subcategory select filtered by category (from `/api/taxonomy`), a password field (remembered in memory for the session, as on the Taxonomy page) and **Save correction**.
- After saving, it shows "Added as an example" (or "Already an example") and the re-check result in green or red.
- The draft and preview parts come in Tasks 6 and 7.

- [ ] **Step 1: Write the failing tests.** Fake `classifier.classify` and `memory._embed`.
  - `test_suggest_returns_labels`
  - `test_suggest_never_creates_ticket` (`db.list_all_tickets() == []` and `db.count_drafts() == 0`)
  - `test_correct_adds_example_and_rechecks`
  - `test_correct_dedupes_example`
  - `test_correct_requires_password`
  - `test_correct_rejects_unknown_category`
  - `test_classify_long_text` (45,000 characters gives 200)
- [ ] **Step 2: Run them and check they fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_classify_page.py tests/test_pages.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement the endpoints, routes, nav and page.**
- [ ] **Step 4: Run all tests and check they pass.** Run `.venv/Scripts/python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** `git commit -m "Classify page: suggest a category for an issue and learn from corrections"`

### Task 6: Draft a definition change and preview it

**Files:**
- Create: `app/definition_drafter.py`
- Modify: `app/classifier.py`: `classify`, `_build_system_prompt`, `_response_schema` and `_retrieve_fewshot` take an optional taxonomy and an excluded text.
- Modify: `app/main.py`: add `POST /api/classify/draft` and `POST /api/classify/preview`.
- Modify: `app/static/index.html`: the draft and preview UI on `#classifyView`.
- Test: `tests/test_definition_draft.py`

**Interfaces:**
- Consumes: Task 1's `Taxonomy.from_data` and `taxonomy.as_dict`; Task 5's `_label`.
- Produces:
  - `classifier.classify(ticket_text, api_key, embed_api_key=None, reporter_hint=None, tax: Taxonomy | None = None, exclude_text: str | None = None)`: when `tax` is set it replaces the global taxonomy in the prompt and the schema. `exclude_text` drops retrieved examples whose collapsed, lower-cased text equals it.
  - `definition_drafter.draft_lines(issue_text: str, wrong_leaf_id: str, right_leaf_id: str, api_key: str) -> dict` returns `{"right_line": str, "wrong_line": str}`.
    - It's one OpenRouter call (`config.OPENROUTER_CLASSIFY_MODEL`, a JSON schema, temperature 0) that is given both subcategories' names and current descriptions plus the issue.
    - After the call, it makes sure `right_line` starts with `"Also route here when"`.
    - It also makes sure `wrong_line` starts with `"NOT this: "` and ends with `" -> {right parent_name} > {right name}."`.
  - `apply_lines(data: dict, right_leaf_id: str, wrong_leaf_id: str, right_line: str, wrong_line: str) -> dict`, in `definition_drafter`: returns `{sub_id: (before, after)}` and edits `data` in place, appending each line with a space. It skips a line already contained in the description.
  - `POST /api/classify/draft {text, wrong_category_id, right_category_id}` (password) returns `{right_line, wrong_line}`. It's a 400 when the two ids are equal or unknown.
  - `POST /api/classify/preview {text, wrong_category_id, right_category_id, right_line, wrong_line}` (password) returns `{"cases": [...]}`:
    - Each case is `{text, expected, before, after, before_ok, after_ok, got_worse}`, where `expected`, `before` and `after` are `_label` dicts.
    - The cases are the issue (expected = right), plus up to 5 existing examples of each of the two subcategories (expected = their own subcategory), excluding the issue itself.
    - "Before" uses the live taxonomy; "after" uses `Taxonomy.from_data(edited copy)`.
    - Example cases run with `exclude_text=<that example>`.
    - The calls run through a `concurrent.futures.ThreadPoolExecutor(max_workers=6)`.
    - `got_worse = before_ok and not after_ok`.
    - Nothing is saved.

**Page:** after a saved correction, a **Suggest a definition change** button shows the two drafted lines (editable textareas) and **Preview**. Preview shows a table with columns Issue / example, Expected, Before, After, where a worse result is a red row. Then come **Apply** and **Skip** (Task 7 wires Apply).

- [ ] **Step 1: Write the failing tests.** Fake the OpenAI client for the drafter and fake `classify`.
  - `test_draft_lines_are_normalised` (a model reply without the prefixes still gets them)
  - `test_draft_rejects_same_category`
  - `test_apply_lines_skips_existing`
  - `test_preview_marks_got_worse`: the fake classify returns the expected id when `tax is None` and a wrong id when `tax` is set, for one example, so that case has `got_worse == True`.
  - `test_preview_saves_nothing` (`taxonomy.version` and the descriptions are unchanged afterwards)
  - `test_classify_uses_given_taxonomy` (the prompt contains a description that exists only in the passed `tax`)
- [ ] **Step 2: Run them and check they fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_definition_draft.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run all tests and check they pass.** Run `.venv/Scripts/python.exe -m pytest -q`. Expected: all pass, including `tests/test_reporter_hint.py::test_model_never_sees_the_reporter_pick`.
- [ ] **Step 5: Commit.** `git commit -m "Classify page: draft a definition change and preview its effect before applying"`

### Task 7: Apply and Undo a definition change

**Files:**
- Modify: `app/db.py`: add the `taxonomy_changes` table and its functions.
- Modify: `app/main.py`: add `POST /api/classify/apply` and `POST /api/classify/undo`.
- Modify: `app/static/index.html`: wire up Apply, the re-check result and Undo.
- Modify: `README.md`: replace the "Pages" section text with Log, Classify and Taxonomy, plus one line on corrections, drafts, preview and undo.
- Test: `tests/test_definition_apply.py`

**Interfaces:**
- Consumes: Task 6's `apply_lines`; Task 1's `taxonomy.save`; Task 5's `_label`.
- Produces:
  - `db.add_taxonomy_change(changes: dict, by: str | None) -> str`: stores `{sub_id: [before, after]}` as JSON and returns the change id.
  - `db.get_taxonomy_change(change_id: str) -> dict | None`
  - `db.mark_taxonomy_change_undone(change_id: str) -> None`
  - `POST /api/classify/apply {text, wrong_category_id, right_category_id, right_line, wrong_line}` (password) applies the lines to `taxonomy.as_dict()`, saves, records the change, re-runs the issue and returns `{change_id, recheck: {..., correct}}`. With nothing to apply (both lines already present) it returns 200 with `change_id: null`.
  - `POST /api/classify/undo {change_id}` (password) restores each `before` only if the subcategory's current description equals its `after`, saves, marks the change undone and returns `{ok: true}`.
    - It's a 409 `"This definition was changed again since - edit it on the Taxonomy page instead."` when any description differs.
    - It's a 404 for an unknown id, and a 409 for one already undone.
- Table: `taxonomy_changes (id TEXT PRIMARY KEY, changes TEXT NOT NULL, created_by TEXT, created_at REAL NOT NULL, undone_at REAL)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_apply_saves_and_records`
  - `test_apply_twice_is_noop`
  - `test_undo_restores`
  - `test_undo_refuses_when_changed_since` (after apply, edit the description via `taxonomy.save` and expect undo to give 409 with the newer text kept)
  - `test_undo_twice_conflicts`
  - `test_apply_requires_password`
- [ ] **Step 2: Run them and check they fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_definition_apply.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run all tests and check they pass.** Run `.venv/Scripts/python.exe -m pytest -q`. Expected: all pass.
- [ ] **Step 5: End-to-end check in the real app.**
  - **Setup:** run locally against a temporary SQLite file with a test webhook secret, following the earlier `e2e_server.py` pattern.
  - **Classify page:** paste "Due to internet issue i am not able to record the session so i got like 0/10", suggest, correct it to another subcategory, then draft, preview, apply and undo.
  - **Log:** send pre-submit and On Add calls, and confirm the timeline and the threshold section.
  - **Persistence:** restart the server and confirm the applied change persists (database, not file).
  - Expected: every step behaves as specified and the server log shows no errors.
- [ ] **Step 6: Commit.** `git commit -m "Classify page: apply or undo a definition change"`
