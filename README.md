# Ticket Router

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)
![OpenAI](https://img.shields.io/badge/OpenAI-structured%20outputs-412991)
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
OpenAI structured-output call            (taxonomy + retrieved examples in prompt)
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
  classifier.py    Builds the dynamic prompt, calls OpenAI, decides finalize vs clarify
  memory.py        Chroma vector store — the "context memory" (seed + corrections)
  taxonomy.py      Loads taxonomy.json, exposes it to the prompt + JSON schema
  taxonomy.json    <-- your real taxonomy (13 categories / 67 routed subcategories)
  db.py            SQLite — ticket/session state, conversation turns, correction log
  models.py        Pydantic request/response + structured-output schema
  static/index.html  Test console UI
```

## Setup

```bash
cd ticket-classifier
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then add your OPENAI_API_KEY
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 — type a ticket, watch it get classified (or asked a
clarifying question), and use "Correct it" to simulate an agent fixing a bad call.

## The taxonomy

`app/taxonomy.json` is wired to the real taxonomy (13 top-level categories,
67 routed subcategories — QA/instructor evaluation, curriculum, staffing/HR,
scheduling, etc.). Classification targets the **subcategory** level, since
that's what actually determines routing (`assigned_team`, `poc_primary`,
`poc_cc`) — the classifier picks a `category_id` like `G01-S01`, and the API/UI
resolve that back to its parent category, team, and point of contact.

Nothing in the code is hardcoded to this taxonomy's content — the classifier,
the JSON schema sent to OpenAI, and the seed examples are all generated from
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
          "poc_cc": "someone-else@company.com",
          "ticket_volume": 33,
          "data_coverage": "Likely Complete",
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
- `assigned_team` / `poc_primary` / `poc_cc` / `ticket_volume` / `data_coverage`
  are optional — omit any of them and the API/UI just won't show that field.
  They're **not** included in the classification prompt (irrelevant to intent,
  and volume figures could bias the model toward common categories), only
  surfaced in responses for routing.
- `examples` are used two ways: they go into the classification prompt for
  their subcategory, and they bootstrap the vector memory on first run
  (`memory.seed_if_empty`) so the system has *something* to retrieve before
  any real corrections exist. A subcategory with zero examples (there's one,
  `G11-S05`) still works — it just relies purely on its `description` until
  corrections start accumulating for it.
- If you edit taxonomy.json after the vector memory has already been seeded,
  call `taxonomy.reload()` (or just restart the service) — existing seed
  embeddings for removed/renamed categories won't be auto-purged; delete
  `app/data/chroma` if you want a clean re-seed.
- The rendered taxonomy block in the prompt is currently ~13K characters
  (~3K tokens) for 67 subcategories — comfortably within context for either
  `gpt-4o-mini` or `gpt-4o`. If you grow well past ~150-200 subcategories,
  consider two-stage classification (pick the category group first, then the
  subcategory within it) to keep each individual prompt smaller and more precise.

## Key config (`.env`)

| Var | Default | Effect |
|---|---|---|
| `OPENAI_CLASSIFY_MODEL` | `gpt-4o-mini` | Model used for classification. Bump to `gpt-4o` for harder taxonomies. |
| `CONFIDENCE_THRESHOLD` | `0.65` | Below this, the ticket needs clarification or human review rather than auto-routing. |
| `MAX_CLARIFICATION_TURNS` | `2` | Caps back-and-forth so the bot doesn't interrogate the user forever; falls back to `needs_human_review`. |
| `FEWSHOT_K` | `5` | How many retrieved examples get injected as dynamic few-shot context per call. |

Tune `CONFIDENCE_THRESHOLD` down if you're getting too many clarifying questions
on tickets a human would consider obvious; tune it up if wrong-but-confident
routes are getting through.

## API

- `POST /api/tickets` `{text}` → classify a new ticket; may return a clarifying question.
- `POST /api/tickets/{id}/respond` `{answer}` → answer a clarifying question, re-classify.
- `POST /api/tickets/{id}/correct` `{corrected_category_id, corrected_by?}` → human correction; **this is what teaches the system**.
- `GET /api/tickets/{id}` → current state + full conversation.
- `GET /api/taxonomy` → current taxonomy (drives the UI's category dropdown).
- `GET /api/stats` → ticket counts by status/category, total corrections — a rough accuracy/volume dashboard starting point.

## Production notes / next steps

- **Storage**: SQLite + local Chroma are fine for one instance. For multi-instance
  production, move `db.py` to Postgres and point Chroma at a hosted instance (or
  swap to Pinecone/Weaviate) — `memory.py` is the only file that would need to change.
- **Auth**: there is none yet. Put this behind your existing auth/gateway before
  exposing `/api/tickets/*` beyond internal use, especially `/correct` (anyone
  who can call it can poison the example bank). This matters more than usual
  here since `taxonomy.json` and every classification response carry real
  employee emails (`poc_primary`/`poc_cc`) — don't expose this API publicly.
- **Correction quality control**: right now any `corrected_by` string is
  accepted at face value. In production you'll want that endpoint restricted to
  verified agents, and probably a review queue for corrections that contradict
  a high volume of prior seed examples, to guard against bad corrections
  degrading the retrieval bank over time.
- **Analytics**: `db.stats()` is a starting point. For real accuracy tracking
  you'll want to periodically sample `classified` tickets for agent QA (not just
  rely on the `correct` endpoint, which only captures cases someone bothered to fix).
- **Observability**: log every `classify()` call's retrieved few-shot set
  alongside the decision — when accuracy dips on a category, you want to see
  exactly what context the model was given, not just the output.
