# Ticket timeline Log + Classify page — design

Date: 2026-10-16. Repo: Zoho_Tickets (FastAPI + static `app/static/index.html`
shell, SQLite locally / Turso in production, Chroma vector memory, OpenRouter
classifier). Today the app has two pages after login: **Log** and
**Taxonomy**.

## Intent

The owner wants two separate tools:

1. **Log**: a plain record of what happened to each ticket, so they can
   understand the back-and-forth with Zoho and spot API errors. It is for
   diagnosis, not for grading tickets.
2. **Classify** (new page): paste an issue, get the suggested category and
   subcategory, and if it's wrong, correct it. A correction becomes a
   learning point, so similar issues route right in future. The long-term
   goal is that tickets route correctly with no human involved.

## Goals

- Every **new** ticket gets a timeline: one line per call or event, in time
  order, searchable by Ticket ID.
- Failures are obvious: a red line with the error details.
- On the Classify page, a correction teaches the model straight away and
  can, when the owner approves, sharpen the taxonomy definitions.
- Every correction made on the Classify page gets checked, so the owner can
  see whether it helped or hurt. *(My call)* Edits made by hand on the
  Taxonomy page are not checked; that page stays as it is apart from the
  storage change.

## Non-goals

- No history for tickets that arrived before this ships. They keep what's
  stored today (the owner said this is fine).
- The Log does not show the raw request/response bodies. A one-line summary
  per call is enough, plus error details on failures (owner's words).
- The Classify page never creates a Zoho ticket and never writes to the Log.
- No grading of real tickets on the Log (the earlier Yes/No idea was
  dropped by the owner in favour of the Classify page).

## Anti-goals (a failure even if it "works")

- A correction that silently makes other tickets route wrong.
- Definition text changing without a person seeing it first.
- Learning that disappears on the next deploy (see Constraints).
- A Log so busy the owner can't tell what went wrong.

## Constraints

- **Corrections need the Taxonomy password** (the owner decided): the same
  `TAXONOMY_EDIT_PASSWORD` and `X-Taxonomy-Password` header the Taxonomy
  editor uses. Trying an issue (getting a suggestion) needs only the normal
  login.
- **Taxonomy changes must survive deploys.** Today `taxonomy.save()` writes
  `app/taxonomy.json` on the server's disk, and each deploy replaces that
  file with the git copy, so any edit made on the live site is lost on the
  next push. The Classify page writes to the taxonomy all the time, so this
  has to be fixed as part of this work: keep the live taxonomy in the
  database (Turso in production) and use `app/taxonomy.json` only as the
  starting copy when the database has none. Editing on the Taxonomy page
  then persists too. *(My call: no other fix keeps the learning.)*
- Existing behaviour that must keep working: the Zoho webhook (pre-submit
  suggestion mode → hidden draft; On Add claims the draft; duplicates
  within 7 days; Zoho transfer → logged as a correction and added to
  memory), the 30-minute auto-classify loop, the reporter threshold
  (`REPORTER_OVERRIDE_MIN_CONFIDENCE`, 0.9), taxonomy save → example sync
  into memory (`_sync_seed_examples`), and login on every page and API
  (the webhook uses its own secret).
- Works on SQLite and Turso: plain SQL, new columns or tables added through
  the existing `_migrate_columns` / `SCHEMA` approach.

## The Log (ticket timeline)

Decided by the owner: one-line summary per call, with error details;
new tickets only.

What's recorded, as events, from now on *(the event list is my call)*:

| Event | When | Summary line shows |
|---|---|---|
| Pre-submit | Zoho form asks for a suggestion (no Ticket ID yet) | instructor's pick → model's pick (confidence), what was sent back, time taken |
| On Add | ticket saved in Zoho | whether the pre-submit result was reused or the ticket was classified again; final category sent back |
| Edit | any later webhook call for the ticket (status change etc.) | what changed (e.g. status) |
| Transfer | Zoho subcategory changed by a person | from → to, "learned from this" |
| Duplicate | same instructor + same text within 7 days | "duplicate of #…" |
| Auto-classify | the 30-minute background job classifies it | pick + confidence |
| Error | any failure: classifier/API error, timeout, rate limit, fallback to `G14-S01`, bad payload | red line with the error message |

- A pre-submit happens before the ticket has an ID. Its events belong to
  the draft row, which becomes the ticket when On Add claims it, so the
  ticket's timeline starts with the pre-submit. A pre-submit nobody
  submitted stays a draft. *(My call)* The Log offers a filter for those
  ("not submitted"), since an error there is still worth seeing.
- Each summary line should read as plain English, e.g. "Model picked
  Campus Network / Wi-Fi Issue (90%) over the instructor's Recording Issue:
  at or above the 90% bar, so it replaced the pick." The override sentence
  must say why, in words.
- **The Override threshold table stays** (the owner decided), as a small
  section at the bottom of the new Log. It's the same table as today (from
  `/api/routing-log` / `app/routing_log.py`'s `learn()`): for each
  confidence value, how many instructor/model disagreements would have
  routed right if the model overrode only at or above it, with the current
  0.9 in bold and the best value in green. The rest of today's Log (the
  per-ticket pick/decision table and the decision tiles) is replaced by
  the timeline list.
- Tickets from before launch still appear in the list *(my call)*, marked
  "no timeline (before this was recorded)".
- Page shape *(my call)*: a search box for Ticket ID, then a list of
  tickets (newest first) showing Ticket ID, time, the issue's first line,
  the final category, and a red marker if anything failed. Clicking a
  ticket opens its timeline. A "has errors" filter, and a date range.

## Classify page (new)

Decided by the owner: issue text only in; suggestion out; wrong → change
category and subcategory; that change is a learning point; saving a
correction asks for the Taxonomy password.

Flow:

1. Paste or type an issue → **Suggest**. The page shows the category,
   subcategory, confidence and the model's one-line reasoning. It runs the
   same classifier the webhook uses, text only (no instructor pick, no
   threshold).
2. **Correct** if wrong: pick the right category and subcategory → enter
   the password → **Save**. On save:
   - **Always:** the issue is added as an example under the chosen
     subcategory (visible on the Taxonomy page) and synced into memory, so
     it takes effect immediately. If the same text is already an example,
     it isn't added twice.
   - **Then, a suggested definition change for approval:** the model
     drafts one or two short lines. Usually that's a "route here when …"
     line for the right subcategory, plus a "NOT this: … → <right
     subcategory>" line on the subcategory it wrongly picked. The owner
     sees the exact text and clicks **Apply** or **Skip**. Nothing in a
     definition changes without that click.
3. **Check** *(details my call)*: checks show results; they never block or
   roll back anything by themselves. The owner stays in control through
   Apply/Skip and Undo.
   - **After the example is saved:** re-run the issue and show whether it
     now routes to the chosen subcategory.
   - **Before Apply:** the drafted definition lines are tried in memory
     first (not saved). The page re-runs the issue and up to 5 existing
     examples from each affected subcategory with and without the draft,
     and shows each one's before → after result. Anything that got worse
     is marked red. The owner then clicks Apply or Skip knowing the
     effect.
   - If the issue still routes wrong, the page says so plainly.
4. **Undo** *(my call)*: an applied definition change can be undone from
   the same screen (restore the previous text). An added example can be
   removed on the Taxonomy page as today.

*(My call)* Header order: Log, Classify, Taxonomy, with Log still the
default page; URL `/classify`.

## Left to the builder

- Event storage schema, the retention policy (keeping everything is fine
  at this volume), and the exact wording of the summary lines, following
  the plain-English rule above.
- The prompt used to draft definition lines, using the existing OpenRouter
  model setting.
- How the database-backed taxonomy and the `taxonomy.json` starting copy
  hand over, including keeping the Taxonomy page's CSV download and
  memory sync working.
- Visual design, following the existing Log/Taxonomy page styles.
- Tests in the existing pytest style.
