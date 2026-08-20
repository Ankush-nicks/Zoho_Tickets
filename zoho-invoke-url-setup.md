# Zoho Creator Invoke URL — Custom API for Ticket Router

## Where this fits in your project

Your portal (`app/zoho.py` + `app/config.py`) already expects to **pull** a
ticket from Zoho by calling an "Invoke URL" and reading back JSON. Nothing on
the Zoho side exists yet — `ZOHO_INVOKE_URL` in `.env` currently points at a
local mock (`/_mock/zoho-invoke` in `app/main.py`) just so the plumbing could
be tested end to end.

What's missing is the actual **Zoho Creator Custom API** (Zoho's name for a
callable "Invoke URL" backed by a Deluge script) that your portal will call
in place of the mock. That's what's below.

```
Your portal (app/zoho.py)  --GET-->  Zoho Creator Custom API (Deluge)  --reads-->  Tickets form
        ZOHO_INVOKE_URL                 (this script)
```

## 1. Create the Custom API in Zoho Creator

In your Creator app: **Settings → Integrations → Custom API → Create Custom API**

- Method: `GET`
- Name it something like `get-ticket`
- Paste the Deluge script below as the function body
- Set **Auth Type**. Zoho Creator gives you a built-in "Private Key" option
  which is simpler than DIY header checking — if you use it, Zoho appends its
  own `privatekey=...` query param to the URL it gives you, and you can drop
  the manual `auth_key` check in the script. The script below does its own
  check too so it works either way (defense in depth, and it matches the
  `ZOHO_AUTH_HEADER_NAME` / `ZOHO_API_KEY` pair your portal's `.env` already
  has slots for).

## 2. The Deluge script

Adjust `Tickets`, `Ticket_ID`, `Issue_in_Detail` to your actual form/field
link names if they differ (Zoho auto-generates link names from display
names — check under the form's field properties if unsure).

```deluge
// Custom API: get-ticket
// Called as: https://creator.zoho.com/api/v2/<account_owner>/<app_link_name>/report/get-ticket
//            ?ticket_id=TCK-1001&auth_key=sample-zoho-key
//
// Returns the same two fields app/zoho.py already looks for:
//   Ticket_ID, Issue_in_Detail
// (these must match ZOHO_FIELD_TICKET_ID / ZOHO_FIELD_ISSUE_DETAIL in .env)

response = Map();

ticket_id = ifnull(input.ticket_id, "");
auth_key  = ifnull(input.auth_key, "");   // swap for Zoho's built-in Private Key auth if you enable it instead

// --- auth check -------------------------------------------------------
if(auth_key != "sample-zoho-key")   // must match ZOHO_API_KEY in the portal's .env
{
	response.put("error", "Unauthorized");
	return response;
}

// --- validate input -----------------------------------------------------
if(ticket_id == "")
{
	response.put("error", "Missing ticket_id parameter");
	return response;
}

// --- look up the record --------------------------------------------------
matchingTickets = Tickets[Ticket_ID == ticket_id];   // "Tickets" = your form's link name

if(matchingTickets.count() == 0)
{
	response.put("error", "Ticket not found: " + ticket_id);
	return response;
}

record = matchingTickets.get(0);

response.put("Ticket_ID", record.get("Ticket_ID"));
response.put("Issue_in_Detail", record.get("Issue_in_Detail"));

return response;
```

## 3. Point the portal at it

Once the Custom API is published, Zoho shows you its live URL — something
like:

```
https://creator.zoho.com/api/v2/<account_owner>/<app_link_name>/report/get-ticket
```

Update `.env` in the ticket-classifier project:

```bash
ZOHO_INVOKE_URL=https://creator.zoho.com/api/v2/<account_owner>/<app_link_name>/report/get-ticket?ticket_id={ticket_id}&auth_key=sample-zoho-key
ZOHO_AUTH_HEADER_NAME=Ticket_Classification_version_0   # only matters if you switch to header-based auth, see note below
ZOHO_API_KEY=sample-zoho-key                             # must equal auth_key check in the Deluge script above
ZOHO_FIELD_TICKET_ID=Ticket_ID
ZOHO_FIELD_ISSUE_DETAIL=Issue_in_Detail
```

The `{ticket_id}` placeholder is replaced by `app/zoho.py` at request time —
keep it in the URL exactly as shown.

**Note on header vs. query-param auth:** `app/zoho.py` currently sends the
key as an HTTP header (`ZOHO_AUTH_HEADER_NAME: ZOHO_API_KEY`). Zoho Creator
Custom APIs read query/form params via `input.<name>` in Deluge, not
arbitrary custom headers, so the script above checks a query param
(`auth_key`) instead. Two ways to reconcile that:

- **Easiest:** bake the key into the URL as a query param (as shown above)
  and stop sending it as a header — the portal side doesn't strictly need to
  change since query params work fine appended to `ZOHO_INVOKE_URL`.
- **More "proper":** turn on Zoho's built-in Private Key auth on the Custom
  API instead of the manual `auth_key` check, and drop the check from the
  script — Zoho then validates the key before your Deluge code even runs.

## 4. Verify against a real response

Once wired up, hit `/zoho-debug` in your portal (`app/static/zoho_debug.html`)
with a real `ticket_id` — it shows the raw JSON Zoho returned so you can
confirm `Ticket_ID` / `Issue_in_Detail` are really the field names coming
back (Zoho's auto-generated link names don't always match the display names
exactly), and adjust `ZOHO_FIELD_TICKET_ID` / `ZOHO_FIELD_ISSUE_DETAIL` in
`.env` if not.

## 5. Push direction — Zoho creates a ticket, portal classifies it automatically

This is the other half: instead of your portal pulling a ticket on demand,
Zoho calls out to `https://zoho-tickets.onrender.com` the moment a new
record is added to the Tickets form, and the portal classifies it right
away — no polling needed.

**Code change (already made for you):** the portal's existing
`/api/zoho/tickets/{id}/classify` endpoint requires a logged-in browser
session, which a Deluge script can't hold. So a new endpoint was added
specifically for this:

```
POST /api/webhooks/zoho/tickets
Headers:  X-Webhook-Secret: <shared secret>
Body:     {"zoho_ticket_id": "...", "issue_in_detail": "..."}
```

It's authenticated by a shared secret instead of a session, and classifies
using the server's own `OPENAI_API_KEY` (no UI operator involved). Changed/added:

- `app/config.py` — new `ZOHO_WEBHOOK_SECRET` env var (empty by default, so
  the endpoint refuses everything until you set it)
- `app/models.py` — new `ZohoWebhookTicket` request model
- `app/main.py` — new `require_webhook_secret` dependency + the
  `POST /api/webhooks/zoho/tickets` route
- `.env.example` — documents both new/required vars

**Before this works you need to, in Render's dashboard (Environment tab):**

1. Set `ZOHO_WEBHOOK_SECRET` to a real random value, e.g. generate one with
   `python -c "import secrets; print(secrets.token_hex(32))"`
2. Set `OPENAI_API_KEY` to a real OpenAI key (this endpoint has no UI
   operator to supply one per-request, unlike the manual/UI flow)
3. Redeploy so both take effect

**Deluge script — attach to the Tickets form's "On Add" workflow**
(Workflow → your form → "On Add of record" → Deluge Script action):

```deluge
// Runs automatically whenever a new record is added to the Tickets form.
// Pushes it straight to the portal for classification.

secretKey = "REPLACE_WITH_THE_SAME_VALUE_YOU_SET_AS_ZOHO_WEBHOOK_SECRET_IN_RENDER";

headerMap = Map();
headerMap.put("Content-Type", "application/json");
headerMap.put("X-Webhook-Secret", secretKey);

paramMap = Map();
paramMap.put("zoho_ticket_id", input.Ticket_ID);
paramMap.put("issue_in_detail", input.Issue_in_Detail);

response = invokeurl
[
	url: "https://zoho-tickets.onrender.com/api/webhooks/zoho/tickets"
	type: POST
	parameters: paramMap.toString()
	headers: headerMap
];

info response;   // shows up in Zoho's Deluge execution log for this workflow run — check it after a test record to confirm success/debug errors
```

Notes:

- `input.Ticket_ID` / `input.Issue_in_Detail` refer to the field link names
  on the form the workflow is attached to — adjust if yours differ.
- Never hardcode `secretKey` in a script you'll share or that other Zoho
  users can view — better practice once you're comfortable with Zoho is to
  store it in Creator's built-in **Secure Preference** store and read it
  back with `zoho.creator.getSecurePreference(...)` instead of a literal
  string. The literal is shown here only to keep the first version simple.
- Test with one manually-created record first and check the Deluge
  execution log (Zoho shows `response` there via the `info` line) — a 401
  there means the secret doesn't match what's set in Render; a 400/500
  means `Ticket_ID`/`Issue_in_Detail` came through empty or Render's
  `OPENAI_API_KEY` isn't set.
- This endpoint always classifies with the server's own `OPENAI_API_KEY` —
  if a ticket needs a clarifying question, that state (`awaiting_clarification`)
  is stored and visible in your portal's UI same as any manual ticket, but
  nothing prompts anyone automatically; someone still has to open the portal
  to answer it.

The pull-direction Custom API (sections 1–4 above) still has its own,
independent use — on-demand lookup of a specific ticket by id — so both can
coexist; you don't have to pick one.
