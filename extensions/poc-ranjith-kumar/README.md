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
6. `zohoReportUrl` opens the Zoho Assigned Tickets report - update it if
   that report URL ever changes.

## Load it in Chrome

1. Go to `chrome://extensions`, enable "Developer mode" (top right).
2. Click "Load unpacked" and select this folder (`extensions/poc-ranjith-kumar/`).
3. Click the extension's toolbar icon to open the side panel.

## "Open in Zoho" search automation

Clicking "Open in Zoho" (on a ticket card or a heat grid chip) reuses an
already-open Zoho tab if one exists (matched by origin, so it works
regardless of which report/record that tab is currently showing) instead
of opening a new tab every click - it re-points that tab at the Assigned
Tickets report, brings its window to the front, and then attempts to
automatically: open Advanced Search, check the "Ticket ID" filter, type
the ticket's Zoho ID into the search field, and click Search - so you land
on that specific ticket instead of the full unfiltered report.

This requires two new permissions (`scripting`, `tabs`) and a host
permission for `niat.zohocreatorportal.in`, since it's a different origin
than this extension's own API. The click sequence lives in
`background.js` (`zohoSearchForTicketId`), built directly from a markup
snippet of that Zoho Creator page - **it has not been verified against
the real, authenticated portal**, since building this extension has no
login there. It polls for each element (search toggle → "Ticket ID"
checkbox → search input → Search button) rather than using a fixed delay,
and fails silently (leaves you on the opened report, filters not applied)
if any step's element isn't found within ~8 seconds - if Zoho's page
structure or field IDs differ from what's captured here, that's the
failure mode to expect. Please try it live and report back if any step
doesn't work as expected.

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
