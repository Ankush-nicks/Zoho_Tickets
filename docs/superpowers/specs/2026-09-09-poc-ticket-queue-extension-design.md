# POC Ticket Queue Chrome Extension — Step 1 (read-only queue popup)

Status: approved for implementation planning
Scope owner (test POC): ranjith.kumar@nxtwave.co.in

## 1. Purpose

Give an individual POC (Point of Contact) a read-only, at-a-glance view of
their currently-open ticket queue — acknowledgement status and SLA
countdown per ticket — without needing to open the main Ticket Router
portal or Zoho itself. This is Step 1 of a larger effort: one small Chrome
extension per POC, additive only. It does not touch classification,
taxonomy, or routing logic, and it never writes back to a ticket.

Test subject for this build: **ranjith.kumar@nxtwave.co.in**.

## 2. Key discovery that reshapes the original brief

The original request assumed `poc_primary`, `poc_cc`, and `assigned_team`
are columns on the `tickets` table. They are not. They live per-subcategory
in `app/taxonomy.json`, and `poc_primary` is a **free-text field**, not a
clean single email:

- Sometimes a single clean email (e.g. G01's three leaves →
  `ranjith.kumar@nxtwave.co.in`)
- Sometimes a comma-separated list of emails (e.g. G03)
- Sometimes a prose placeholder with no email at all (e.g. "Respective
  Capability Manager", "Respective Campus COS")

Searching the full taxonomy, `ranjith.kumar@nxtwave.co.in` appears as
`poc_primary` on exactly three leaves — **G01-S01, G01-S02, G01-S03**,
all under group **G01 "QA Report / Instructor Evaluation"** — and nowhere
else. So this POC's entire queue is, today, scoped to G01 tickets. The
matching logic implemented here is general-purpose (comma-split, trim,
case-insensitive containment) so the next POC's extension works correctly
even though their email may appear inside a multi-person list — this
build is not hardcoded to exact-string-equals-G01.

Two further gaps discovered and resolved with the user during brainstorming:

- **No numeric SLA deadline field exists anywhere in Zoho's payload.**
  Only a categorical `sla_breach_status` string (e.g. "Within SLA" /
  "Breached") is sent — never a due-by timestamp. SLA countdowns are
  therefore always computed from `created_at + category SLA hours`, never
  read from Zoho.
- **No clean acknowledgement timestamp exists either.** Zoho sends only
  free-text ack fields (`acknowledgement_from_the_poc`,
  `acknowledgement_history`) consumed today by the resolution-grading LLM
  at ticket-close time. A new `acknowledged_at` column is added per the
  original brief, but nothing populates it yet — **every ticket will show
  as not-yet-acknowledged in this build.** Accepted explicitly as a known
  Step 1 limitation; a future step adds a real write path (e.g. a Zoho "On
  Edit" workflow firing the moment the ack field first goes non-empty).
- **No browser-viewable Zoho record URL exists** — only `ZOHO_INVOKE_URL`,
  a private API endpoint with an auth key baked into its query string,
  never safe to expose as a clickable link. **"Open in Zoho" is dropped
  from this build entirely** (not built disabled, not stubbed — simply
  absent) until a real, safe view-URL pattern is supplied.
- Zoho's real priority field is `priority_level`, valued P1/P2/P3/P4
  (confirmed via existing pulse-dashboard code), not High/Medium/Low as
  in the original brief.

## 3. SLA / priority table

Per-ticket priority displayed on a card is Zoho's own `priority_level`
verbatim (omitted if absent) — it does not drive SLA math.

SLA hours are looked up by the ticket's top-level taxonomy group code,
since only that granularity is confirmed unambiguous right now:

| Group | Name | SLA hours |
|---|---|---|
| G01 | QA Report / Instructor Evaluation | 24 |

Only G01 is reachable by `ranjith.kumar@nxtwave.co.in`, so only this row
is populated for Step 1. A configurable default (48h) applies to any
group without an explicit entry. The user separately supplied SLA/priority
figures for several other issue types (Content, Scheduling, Facilities,
Leave/WFH, etc.) during brainstorming, but two of them ("Content" vs.
"Cirriculam" under G02; "Scheduling" vs. "Session disruption" under G05)
implied a split finer than the group level that isn't reachable by this
POC and is deliberately left unresolved — a question for whichever future
POC extension needs those groups.

## 4. Data model changes

`app/db.py` — add to `SCHEMA` and to the existing ALTER-TABLE-if-missing
loop in `init_db()`:

```sql
acknowledged_at REAL   -- nullable; unpopulated in this step, see §2
```

No other schema change. No change to `turns` or `corrections` tables, to
`taxonomy.json`, or to `taxonomy.py`.

## 5. Config additions (`app/config.py`)

```python
# One env var per POC for now (matches ADMIN_USERNAME/PASSWORD,
# ZOHO_WEBHOOK_SECRET's existing pattern) - a small dict, not a table,
# since only one POC extension exists today.
POC_TOKENS = {
    "ranjith.kumar@nxtwave.co.in": _env("POC_TOKEN_RANJITH_KUMAR"),
}

ACK_WINDOW_HOURS = 4.0
SLA_AT_RISK_FRACTION = 0.15   # last 15% of the SLA window counts as "at risk"
ACK_URGENT_MINUTES = 15       # last 15 minutes of the ack window counts as urgent

CATEGORY_SLA_HOURS = {
    "G01": 24.0,
}
CATEGORY_SLA_HOURS_DEFAULT = 48.0
```

## 6. New endpoint: `GET /api/extension/my-tickets`

**Auth:** new `require_poc_token` dependency in `app/main.py`, modeled on
the existing `require_webhook_secret` pattern — reads header
`X-POC-Token`, looks it up against `config.POC_TOKENS` (reverse map
token → email), 401s on missing/unknown token. Independent of the
session-cookie login the main portal UI uses; this is for a
non-browser-session extension context.

**Ticket selection**, over `db.list_all_tickets()`:
1. `status in ("classified", "corrected")` — matches the user's choice to
   exclude `awaiting_clarification` / `needs_human_review` tickets, which
   have no confident category yet.
2. `taxonomy.get(ticket["category_id"])` resolves to a leaf whose
   `poc_primary`, split on commas and trimmed, contains this POC's email
   (case-insensitive).
3. Not already closed: `raw_payload.get("ticket_status")` is either
   absent (never came from Zoho — always treated as open) or not a
   member of the existing `quality_scorer.CLOSED_STATUSES` set
   (`{"Resolved By POC", "Resolution Acknowledged"}`) — reusing that set
   rather than defining a second, possibly-drifting definition of
   "closed."

**Per-ticket resolved fields returned:**

```
id                   internal ticket id
zoho_ticket_id
category_group_code  e.g. "G01"          (from taxonomy leaf's parent_id)
category_group_name  e.g. "QA Report / Instructor Evaluation" (parent_name)
priority              raw_payload.priority_level, or omitted if absent
created_at            epoch seconds

ack_deadline_at       created_at + ACK_WINDOW_HOURS
acknowledged_at        always null in this step (see §2)
ack_state              "acknowledged" | "pending" | "missed"
ack_urgent             bool - true only when pending AND <= ACK_URGENT_MINUTES remain

sla_hours              resolved from CATEGORY_SLA_HOURS[group] or the default
sla_deadline_at         created_at + sla_hours * 3600
sla_state               "on_track" | "at_risk" | "breached"
sla_overdue_seconds     present only when sla_state == "breached"
```

Plus a `summary` object: `{breached, needs_ack_now, on_track}` counts
across the returned list, where `needs_ack_now` = `ack_state in
("pending","missed")` with `ack_urgent` true, or `ack_state == "missed"`.

State computation happens once, at request time (server clock), matching
the non-goal of no background polling / no live countdown — the popup
re-fetches only when reopened.

**Sort** is entirely client-side over the one fetched array (no refetch
per tab):
- **Risk** (default): `sla_state == breached` first, then `ack_state ==
  missed`, then `ack_urgent`, then the rest ordered by ascending
  `sla_deadline_at - now` (soonest-to-breach first among the remainder).
- **Priority**: P1 → P2 → P3 → P4 → no-priority-set last.
- **SLA left**: ascending `sla_deadline_at - now` — this single ordering
  naturally puts the most-overdue ticket (most negative) at the very top,
  followed by less-overdue, then soonest-to-breach, then everything else.

## 7. Extension

Manifest V3, one folder per POC per the user's direction:
`extensions/poc-ranjith-kumar/manifest.json`, `popup.html`, `popup.js`,
`popup.css`. No background service worker — a single `fetch()` on popup
open is enough; nothing here needs to run when the popup is closed
(explicit non-goal: no badge counts, no background polling).

The POC token for this specific build lives in a small `config.js` in the
extension's own folder (one extension = one POC = one baked-in token;
this is not a multi-POC extension, so no runtime login/config UI).

**Layout**, matching the visual reference and the dataviz-style pill
convention already used elsewhere in this codebase (red = breached/missed,
amber = at-risk/due-soon, teal = on-track/healthy):
- Summary row: three counts (out-of-SLA, needs-ack-now, on-track).
- Three sort tabs (Risk / Priority / SLA left) — instant client re-render,
  no network call.
- Card list: Ticket ID prominent (monospace); category as
  `"G01 · QA Report / Instructor Evaluation"` small/secondary; priority pill
  if present; an ack-status line (acknowledged / due-in / missed, styled
  per §6); an SLA progress bar/label (on-track / at-risk / breached, with
  overdue duration when breached). No outbound action element in this
  step (see §2 — "Open in Zoho" is out of scope, not merely hidden).

## 8. Error handling

- Missing/invalid `X-POC-Token` at fetch time → popup shows a plain
  "extension isn't configured correctly" message, not a blank popup.
- Network/server failure → a retry-affordanced error state.
- Zero matching tickets → an explicit empty state ("queue is empty"),
  distinguishable from an error.

## 9. Explicit non-goals (unchanged from the original brief)

No acknowledge button or any write action. No push notifications, badge
counts, or background polling. No content-script injected into Zoho's own
pages. No Teams/Outlook/external notification channel. No load-balancing,
escalation, or nudge logic. No changes to the classifier, taxonomy, or
existing API routes. "Open in Zoho" additionally dropped for this step
specifically (see §2).

## 10. Testing

- Backend: endpoint tests seeding tickets across the ack/SLA state
  boundaries — just-acknowledged-window-open, ack-window-missed-by-a-minute,
  SLA-at-risk-boundary, SLA-just-breached — asserting computed
  `ack_state`/`sla_state`/`sla_overdue_seconds` and each sort mode's
  resulting order. Also: a ticket whose `poc_primary` is a comma-separated
  list containing this POC's email is correctly included; a ticket routed
  to a different/placeholder POC string is correctly excluded; a
  `needs_human_review` ticket is excluded regardless of category.
- Extension: manual load-unpacked verification against the local dev
  server (no extension test harness exists in this repo today).

## 11. Acceptance check (revised from the original brief)

- Loads unpacked in Chrome, popup opens, shows only
  `ranjith.kumar@nxtwave.co.in`'s open (G01) tickets.
- Every card shows one of acknowledged/pending/missed and one of
  on-track/at-risk/breached, matching the math in §6.
- Switching sort tabs reorders the visible list instantly, no refetch.
- No existing route, table, taxonomy file, or classifier/routing logic
  was modified.
- ~~"Open in Zoho" opens the real ticket in a new tab~~ — dropped for
  this step (see §2); to be added once a real record-view URL pattern is
  supplied.
