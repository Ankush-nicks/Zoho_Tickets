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
