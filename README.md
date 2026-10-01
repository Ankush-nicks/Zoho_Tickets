# Ticket Router

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)
![OpenRouter](https://img.shields.io/badge/OpenRouter-structured%20outputs-6467f2)
![Chroma](https://img.shields.io/badge/vector%20store-Chroma-orange)

A dynamic, learning ticket classifier: a ticket comes in, it's matched against your
taxonomy, and if the intent is genuinely ambiguous the system asks a follow-up
question before committing to a route. Every human correction is fed back into a
vector memory so future, similar tickets are classified more accurately — no
retraining or redeploy required.

## How it works

```
ticket text
     │
     ▼
retrieve K most-similar known examples  (Chroma vector store: taxonomy seed
     │                                    examples + past corrected tickets)
     ▼
OpenRouter structured-output call        (taxonomy + retrieved examples in prompt)
     │
     ▼
confident & unambiguous? ──No──► ask ONE clarifying question ──► append answer,
     │                                                             re-run loop
    Yes                                                            (capped by
     │                                                     MAX_CLARIFICATION_TURNS)
     ▼
route ticket + store category/confidence/reasoning
     │
     ▼
agent disagrees? ──► POST /correct ──► embed (text → correct category)
                                        into vector memory immediately
                                        = next similar ticket gets it right
```

This is "dynamic" in the practical sense that matters for a classifier running on
an API model you don't fine-tune: **in-context learning from a growing, retrieved
example bank**, not weight updates. It gets better as corrections accumulate,
with no deploy step.

## Project layout

```
app/
  main.py          FastAPI routes — the classify/clarify/correct orchestration
  classifier.py    Builds the dynamic prompt, calls OpenRouter, decides finalize vs clarify
  memory.py        Chroma vector store — the "context memory" (seed + corrections)
  taxonomy.py      Loads taxonomy.json, exposes it to the prompt + JSON schema
  taxonomy.json    <-- your real taxonomy (14 categories / 68 routed subcategories)
  db.py            SQLite (local) / Turso (prod) — ticket/session state, conversation turns, correction log
  models.py        Pydantic request/response + structured-output schema
  static/index.html  Test console UI
```

## Setup

```bash
cd ticket-classifier
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # optional overrides only, see below
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` matters on Replit specifically - its webview/deployment can't reach a
server bound to `127.0.0.1`. See `REPLIT_DEPLOY.md` for deploying this as an always-on
Reserved VM instead of running it locally.

Set `OPENROUTER_API_KEY` (classification/grading) and `OPENAI_API_KEY` (embeddings
only, see below) in `.env` before starting the server - there's no in-browser key
input, both are server-side only. Open http://localhost:8000, log in, then type a
ticket and watch it get classified (or asked a clarifying question), and use
"Correct it" to simulate an agent fixing a bad call.

## The taxonomy

`app/taxonomy.json` is wired to the real taxonomy (14 top-level categories,
67 routed subcategories — QA/instructor evaluation, curriculum, staffing/HR,
scheduling, etc., plus a catch-all "Other / Unclear" category for tickets with
no discernible intent). Classification targets the **subcategory** level, since
that's what actually determines routing (`assigned_team`, `poc_primary`)
— the classifier picks a `category_id` like `G01-S01`, and the API/UI
resolve that back to its parent category, team, and point of contact.

Nothing in the code is hardcoded to this taxonomy's content — the classifier,
the JSON schema sent to OpenRouter, and the seed examples are all generated from
this file at startup. To update it, re-export the same 2-level shape:

```json
{
  "version": 2,
  "categories": [
    {
      "id": "G01",
      "name": "QA Report / Instructor Evaluation",
      "subcategories": [
        {
          "id": "G01-S01",
          "name": "Feedback Too Generic or Vague",
          "description": "1-3 sentences the model uses to distinguish this from other subcategories.",
          "assigned_team": "IAS/SET",
          "poc_primary": "someone@company.com",
          "examples": ["a real historical ticket", "another one"]
        }
      ]
    }
  ]
}
```

- Leaf `id` (e.g. `G01-S01`) is what gets stored/routed/corrected on — keep it
  stable once you're in production; renaming an id orphans old corrections
  tied to it.
- `assigned_team` / `poc_primary` are optional — omit either and the API/UI
  just won't show that field. They're **not** included in the classification
  prompt (irrelevant to intent), only surfaced in responses for routing.
  `poc_primary` also drives the POC queue — a subcategory without it won't
  show up in any POC's queue.
- `examples` are not put in the classification prompt directly - they
  bootstrap the vector memory on first run (`memory.seed_if_empty`), and the
  most similar ones are retrieved into the prompt as few-shot context, so
  the system has *something* to retrieve before any real corrections exist. A subcategory with zero examples (there's one,
  `G11-S05`) still works — it just relies purely on its `description` until
  corrections start accumulating for it.
- If you edit taxonomy.json's examples after the vector memory has already
  been seeded, the service keeps retrieving the old seed examples until you
  run `python scripts/reseed_memory.py` on the deployed instance. It swaps
  only the seed entries for the current taxonomy.json ones and keeps every
  correction — don't delete `app/data/chroma` for this, that loses them all.
- The rendered taxonomy block in the prompt is currently ~13K characters
  (~3K tokens) for 68 subcategories — comfortably within context for either
  `gpt-4o-mini` or `gpt-4o`. If you grow well past ~150-200 subcategories,
  consider two-stage classification (pick the category group first, then the
  subcategory within it) to keep each individual prompt smaller and more precise.

### Uploading ticket data (Taxonomy tab)

Once the Taxonomy tab is unlocked (same `TAXONOMY_EDIT_PASSWORD`), **Upload
tickets CSV** takes a Zoho "Instructors Ticketing System" export and upserts
it by `Ticket ID` (`POST /api/tickets/import-csv`). It previews the counts
first and writes only after you confirm.

- New Ticket IDs are created dated by their Zoho "Added Time", with their
  category taken from Zoho's own subcategory - they're already categorised
  there, so they're never sent to the classifier (no credits used). A
  subcategory that isn't in the taxonomy goes to human review instead.
  Tickets left `pending` by an earlier upload are settled the same way at
  startup and before every classify batch.
- Known Ticket IDs get their Zoho data refreshed: every CSV column, the
  category/subcategory and the issue text. Their classification and any
  human correction are left untouched.
- A ticket whose stored Zoho data is newer than the row's "Modified Time"
  (the live webhook already delivered a later edit) is left as-is - an
  export is a snapshot and never rolls a ticket back.
- `Ticket ID` and `Issue In Detail` columns are required; rows missing
  either, and "Dummy" (test) category rows, are skipped and listed. Extra
  columns are kept too.
- Writes run in the background in small committed chunks with progress
  shown, so the live webhook isn't blocked. If an import is interrupted,
  re-upload the same file: it only writes what's missing, never duplicates.

### Daily Issue Check tab

Drill down tickets by category → subcategory → AI-made groups, spot problem
patterns, and run a daily check of new tickets against known problems.
`app/static/daily-issue-check.html` is shown in a frame inside the app and
loads every Zoho ticket from the database (`GET /api/daily-issue/tickets`).
Its AI features (summarise, split into groups, spot new issues, ask a
question, describe issues, sort new tickets) call OpenRouter through the
server (`/api/daily-issue/ai/text` streamed, `/api/daily-issue/ai/json`),
with the model set by `OPENROUTER_DAILY_ISSUE_MODEL` (default
`openai/gpt-4o-mini`). They only run when someone clicks them. Groups,
remarks and descriptions you make are saved in your browser, not the
database.

## Key config (`.env`)

| Var | Default | Effect |
|---|---|---|
| `OPENROUTER_API_KEY` | (required) | Key for classify()/grade_resolution() - get one at openrouter.ai/keys. |
| `OPENROUTER_CLASSIFY_MODEL` | `openai/gpt-4o-mini` | Model used for classification + resolution grading, in OpenRouter's `provider/model` form. |
| `OPENAI_API_KEY` | (required) | Used ONLY for embeddings now (few-shot memory) - OpenRouter has no embeddings endpoint. |
| `CONFIDENCE_THRESHOLD` | `0.65` | Below this, the ticket needs clarification or human review rather than auto-routing. |
| `MAX_CLARIFICATION_TURNS` | `2` | Caps back-and-forth so the bot doesn't interrogate the user forever; falls back to `needs_human_review`. |
| `FEWSHOT_K` | `5` | How many retrieved examples get injected as dynamic few-shot context per call. |
| `FEWSHOT_REPORTER_K` | `2` | Extra few-shot examples pulled from the instructor's picked category (see below). |
| `REPORTER_OVERRIDE_MIN_CONFIDENCE` | `0.95` | How sure the model must be to override the subcategory the instructor picked. |

Tune `CONFIDENCE_THRESHOLD` down if you're getting too many clarifying questions
on tickets a human would consider obvious; tune it up if wrong-but-confident
routes are getting through.

### The instructor's own category pick

Instructors pick a category (and optionally a subcategory) on the Zoho form
before the pre-submit script classifies and overwrites them. That pick often
carries intent the issue text leaves out, so the webhook passes it to
`classify()` as a `ReporterHint`:

- it's shown to the model as a `REPORTER-SELECTED CATEGORY` line with a
  "strong prior" rule, and few-shot retrieval adds examples from that
  category's subcategories;
- `apply_reporter_prior()` then keeps the instructor's subcategory unless
  the model picks a different one with at least
  `REPORTER_OVERRIDE_MIN_CONFIDENCE` - agreement skips clarification;
- a category-only pick narrows the model toward that group (prompt only);
  an "Other / Unclear" pick is ignored;
- if classification fails outright (API key/credits exhausted, network
  error), the webhook returns the instructor's own category and subcategory
  unchanged, with `needs_review: true`, instead of the catch-all fallback.

The pre-submit Deluge script must send `sub_category_of_the_issue` for this
to see the subcategory (see `Claude outputs/zoho_invoke_script_fixed.deluge`).

Each ticket is classified **once**, at pre-submit. The On Add call that
follows stores that same result (remembered for an hour by issue text, so
its confidence/reasoning carry over) or, failing that, the category the
form was saved with - it only calls the model again when the pre-submit
classification itself failed or the saved subcategory isn't usable.

### OpenRouter vs Gemini accuracy comparison

The app's own classify path goes through OpenRouter, but `app/classifier.py`
also has `classify_gemini()` - same taxonomy, prompt, and few-shot retrieval,
just a Gemini call instead for the final decision. `scripts/compare_
classifiers.py` runs both against existing tickets that already carry a Zoho
category tag (used as a rough reference, not a hand-labeled eval set) and
reports agreement rates:

```bash
OPENROUTER_API_KEY=sk-or-... OPENAI_API_KEY=sk-... GEMINI_API_KEY=... python scripts/compare_classifiers.py --limit 20
```

`OPENAI_API_KEY` is only needed here for embeddings (few-shot retrieval used
by both sides), not classification. Needs `GEMINI_API_KEY` (and
`GEMINI_CLASSIFY_MODEL`, default `gemini-2.5-flash`) set too - see
`.env.example`. Not wired into the running app in any way; it's a standalone
script for deciding whether switching or dual-running models is worth
pursuing further.

### Cloudflare AI Gateway (optional)

Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_AI_GATEWAY_ID` (see `.env.example`)
to route OpenAI embedding calls (memory.py) through a Cloudflare AI Gateway
instead of hitting OpenAI directly - same `OPENAI_API_KEY`, same model, but
Cloudflare caches repeated identical requests and gives a usage dashboard at
`dash.cloudflare.com -> AI -> AI Gateway`. Does **not** apply to classify()/
grade_resolution() (those go to OpenRouter, not OpenAI). Unset means
embedding calls go straight to OpenAI, unchanged from before this existed.

Separately, set `CLOUDFLARE_API_TOKEN` (a token with **Workers AI** permission
specifically, not AI Gateway's) and `classify()`/`grade_resolution()`
automatically fall back to Cloudflare Workers AI (`CLOUDFLARE_WORKERS_AI_MODEL`,
default `@cf/meta/llama-3.1-8b-instruct`) whenever OpenRouter raises a
rate-limit error - a completely separate quota from OpenRouter's, so
classification/grading keeps working instead of stalling until OpenRouter's
own limit resets. Same taxonomy/prompt/few-shot pipeline either way; only the
final model call moves. Unset means these calls behave exactly as before
(raise on rate limit).

## API

All routes below (and the UI itself) require an admin session — log in at
`/login` first (`POST /api/login` `{username, password}` sets the session
cookie; `POST /api/logout` clears it).

- `POST /api/tickets` `{text}` → classify a new ticket; may return a clarifying question.
- `POST /api/tickets/{id}/respond` `{answer}` → answer a clarifying question, re-classify.
- `POST /api/tickets/{id}/correct` `{corrected_category_id, corrected_by?}` → human correction; **this is what teaches the system**.
- `GET /api/tickets/{id}` → current state + full conversation.
- `GET /api/taxonomy` → current taxonomy (drives the UI's category dropdown).
- `GET /api/tickets/range?date_from=&date_to=` → every ticket (full state incl. `raw_payload`) in an optional UTC date range (all-time if omitted) — the Stats tab fetches this once per range and does all faceted filtering/grouping (by category, university, SLA status, priority, team, etc.) client-side.

## Production notes / next steps

- **Storage**: SQLite locally, Turso (remote libSQL) in production (set
  `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` - see `persistent-storage-
  setup.md`). Turso speaks the same SQL as SQLite, so `db.py` runs
  unchanged against either. Reads are server-side filtered
  (`db.list_pending_tickets`, `db.count_pending_tickets`,
  `db.list_tickets_by_raw_status`) rather than reading the whole ticket
  history, cheaper than a full table scan on every background-loop cycle.
  Chroma is still local-only; for multi-instance production point it at a
  hosted instance (or swap to Pinecone/Weaviate) — `memory.py` is the only
  file that would need to change.
- **Hosting**: see `REPLIT_DEPLOY.md` for running this as an always-on
  Replit Reserved VM instead of Render's free plan — no cold starts, and the
  two background loops (auto-classify, auto-score) keep running between
  requests, which an autoscale-to-zero host would kill.
- **Auth**: a single admin login (session cookie, set via `ADMIN_USERNAME`/
  `ADMIN_PASSWORD` in `.env`, defaulting insecurely to `admin`/`admin` if unset)
  gates the UI and every `/api/*` route. This is fine for one trusted operator;
  for multiple real agents/reviewers you'll want per-user accounts (a real
  `users` table + password hashing) instead of one shared credential, since
  `db.log_correction`'s `corrected_by` is just a free-text string right now and
  can't be trusted to identify who actually made a change.
- **Correction quality control**: right now any `corrected_by` string is
  accepted at face value. In production you'll want that endpoint restricted to
  verified agents, and probably a review queue for corrections that contradict
  a high volume of prior seed examples, to guard against bad corrections
  degrading the retrieval bank over time.
- **Analytics**: the Stats tab / `GET /api/tickets/range` is a starting point. For real accuracy tracking
  you'll want to periodically sample `classified` tickets for agent QA (not just
  rely on the `correct` endpoint, which only captures cases someone bothered to fix).
- **Observability**: log every `classify()` call's retrieved few-shot set
  alongside the decision — when accuracy dips on a category, you want to see
  exactly what context the model was given, not just the output.
