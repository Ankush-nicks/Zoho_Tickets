# Ticket Queue extension — ranjith.kumar@nxtwave.co.in

Read-only side panel showing this POC's open QA Report tickets. See
`docs/superpowers/specs/2026-09-09-poc-ticket-queue-extension-design.md`
for the full design.

Runs as a Chrome **side panel** (not a popup) so it can use the full height
of the browser window - click the extension's toolbar icon to open/close it.
Chrome itself decides which edge of the window the panel docks to (some
Chrome versions let the user move this in Settings); the extension has no
control over left vs. right placement.

## One-time setup

1. Generate a token: `python -c "import secrets; print(secrets.token_hex(32))"`
2. Add it to the server's `.env`: `POC_TOKEN_RANJITH_KUMAR=<the token>`
3. Restart the server so the new env var takes effect.
4. Copy `config.example.js` to `config.js` in this same folder, then paste
   the token into the copy (`token` field). `config.js` is gitignored -
   never commit a real token.
5. Set `config.js`'s `apiBaseUrl` to wherever the server runs
   (`http://localhost:8000` for local dev, or the deployed URL), and
   `pocDisplayName` to the name shown in the header.
6. `zohoReportUrl` opens this Zoho report (currently "All Instructors
   Ticketing System Report") - update it if that report URL ever changes.

## Load it in Chrome

1. Go to `chrome://extensions`, enable "Developer mode" (top right).
2. Click "Load unpacked" and select this folder (`extensions/poc-ranjith-kumar/`).
3. Click the extension's toolbar icon to open the side panel.

## "Open in Zoho" search automation

Clicking "Open in Zoho" (on a ticket card or a heat grid chip) reuses an
already-open Zoho tab if one exists (matched by origin, so it works
regardless of which report/record that tab is currently showing) instead
of opening a new tab every click - it re-points that tab at the
configured report, brings its window to the front, and then attempts to
automatically: open Advanced Search, check the "Ticket ID" filter, type
the ticket's Zoho ID into the search field, select it from the resulting
autocomplete dropdown (Ticket ID is a select2 lookup field, not free text
- typing alone doesn't count as a search criterion until a suggestion is
clicked), and click Search - so you land on that specific ticket instead
of the full unfiltered report.

This requires two new permissions (`scripting`, `tabs`) and a host
permission for `niat.zohocreatorportal.in`, since it's a different origin
than this extension's own API. The click sequence lives in
`background.js` (`zohoSearchForTicketId`), and **has since been verified
live against the real, authenticated portal** (searching ticket 2590
end-to-end). It polls for each element (search toggle → "Ticket ID"
checkbox → search input → Search button) rather than using a fixed delay,
and fails silently (leaves you on the opened report, filters not applied)
if any step's element isn't found within ~8 seconds.

Selecting the autocomplete suggestion needed real debugging: select2 v3
binds selection to a `mousedown`/`mouseup`/`click` sequence on the
result's inner `.select2-result-label`, not a plain `click` on the
suggestion `<li>` - and `HTMLElement.click()` (what earlier versions of
this used) only ever fires a `click` event, never `mousedown`/`mouseup`,
so select2 never actually registered a selection attempt. The fix
dispatches the full three-event sequence with real `MouseEvent`
properties (`button`/`buttons`/`which`/`clientX`/`clientY`) instead.

The autocomplete-dropdown step specifically waits for the suggestion list
to *settle* (no "searching" indicator active, and the same candidate
option's text unchanged for ~350ms), not just for an `<li>` to exist -
select2 renders a "Searching…" placeholder immediately and replaces it
once its lookup actually resolves, so reading the list too early clicks
the placeholder instead of the real match (this was the cause of an
earlier version only working on the 2nd or 3rd click). Please try it live
and report back if any step doesn't work as expected.

## Acknowledgement status

`acknowledged_at` (the db column) is still never written by anything - but
the queue now resolves real acknowledgement from Zoho's own data instead of
always showing "not yet acknowledged": it parses the first "Updated On"
timestamp out of `raw_payload.acknowledgement_history` (a real, consistently
formatted line Zoho includes in that field), falling back to the ticket's
`updated_at` on the rare ticket that has ack text but no parseable
timestamp in it, and only shows "not yet acknowledged" when there's truly
no ack text logged at all. See `app/poc_queue.py`'s `_resolve_acknowledged_at`.

## Known limitations (Step 1)

- Tickets Zoho marks "Closed" are not currently recognized as closed by this
  queue (only "Resolved By POC" and "Resolution Acknowledged" are) - they
  may continue to appear here after being closed in Zoho.
