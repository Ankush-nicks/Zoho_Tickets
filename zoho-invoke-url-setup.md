# Zoho Creator setup for Ticket Router

Zoho pushes every ticket to the portal; the portal never pulls from Zoho.

## Push webhook — Zoho creates a ticket, portal classifies it automatically

Zoho calls out to `https://zoho-tickets-1rrw.onrender.com` the moment a new
record is added to the Tickets form, and the portal classifies it right
away — no polling needed.

**Code change (already made for you):** the portal's existing
`/api/zoho/tickets/{id}/classify` endpoint requires a logged-in browser
session, which a Deluge script can't hold. So a new endpoint was added
specifically for this:

```
POST /api/webhooks/zoho/tickets
Headers:  X-Webhook-Secret: <shared secret>
Body:     {"zoho_ticket_id": "...", "issue_in_detail": "...",
           "category_of_the_issue": "...", "sub_category_of_the_issue": "..."}
Response: {"ok": true}   (200) — classification runs server-side; the result
                            itself isn't sent back to Zoho, it just shows up
                            in this portal's UI. Zoho only needs the status
                            code: 200 = accepted, anything else = check the
                            error body (401 bad secret, 400 empty text,
                            500 server misconfigured).
```

The endpoint stores whatever the Deluge script above actually sends in
`tickets.raw_payload` - only
`zoho_ticket_id`/`issue_in_detail` are required; everything else
(`category_of_the_issue`/`sub_category_of_the_issue` and the analytics
fields below them) is optional and just rides along unused by
classification itself. `category_of_the_issue`/`sub_category_of_the_issue`
specifically feed the portal's Zoho-agreement comparison (does our model's
prediction match whatever category the ticket already carried in Zoho) -
a rough, automatic accuracy signal from real traffic.

It's authenticated by a shared secret instead of a session, and classifies
using the server's own `OPENROUTER_API_KEY` (no UI operator involved). Changed/added:

- `app/config.py` — new `ZOHO_WEBHOOK_SECRET` env var (empty by default, so
  the endpoint refuses everything until you set it)
- `app/main.py` — new `require_webhook_secret` dependency + the
  `POST /api/webhooks/zoho/tickets` route. Takes the payload as a plain dict
  rather than a typed model on purpose - Zoho's real "On Add" workflow sends
  dozens of fields, and the whole thing is kept as-is in the `tickets.raw_payload`
  column for analytics, so this endpoint never needs a code change when
  Zoho's form gains a field. Only zoho_ticket_id/issue_in_detail/
  category_of_the_issue/sub_category_of_the_issue are pulled out specifically.
- `.env.example` — documents both new/required vars

**Before this works you need to, in Render's dashboard (Environment tab):**

1. Set `ZOHO_WEBHOOK_SECRET` to a real random value, e.g. generate one with
   `python -c "import secrets; print(secrets.token_hex(32))"`
2. Set `OPENROUTER_API_KEY` to a real OpenRouter key, and `OPENAI_API_KEY`
   to a real OpenAI key for embeddings (this endpoint has no UI operator to
   supply one per-request, unlike the manual/UI flow)
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
paramMap.put("category_of_the_issue", input.Category_Of_The_Issue);       // optional - for the portal's Zoho-agreement comparison only
paramMap.put("sub_category_of_the_issue", input.Sub_Category_Of_The_Issue); // optional - adjust link names if yours differ
// Everything below is optional too - stored with the ticket, never used
// for classification itself. Link names below
// follow Zoho's default space-to-underscore convention (matching
// Category_Of_The_Issue/Sub_Category_Of_The_Issue above) - open this
// form in Zoho Creator's builder and check each field's "Link Name" in
// its properties panel if any of these don't resolve.
paramMap.put("university_boa", input.University);        // the campus/university name - despite the key name, this reads Zoho's "University" field, not the separate (usually empty) "University BOA" field
paramMap.put("ticket_status", input.Ticket_Status);
paramMap.put("assigned_team", input.Assigned_Team);
paramMap.put("sla_breach_status", input.SLA_Breach_Status);
paramMap.put("is_it_a_recurring_issue", input.Is_it_a_recurring_issue);
paramMap.put("department_name", input.Department_Name);
paramMap.put("priority_level", input.Priority_Level);
paramMap.put("campus_city", input.Campus_City);

response = invokeurl
[
	url: "https://zoho-tickets-1rrw.onrender.com/api/webhooks/zoho/tickets"
	type: POST
	parameters: paramMap.toString()
	headers: headerMap
];

info response;   // shows up in Zoho's Deluge execution log for this workflow run — check it after a test record to confirm success/debug errors
```

Notes:

- `input.Ticket_ID` / `input.Issue_in_Detail` (and every other `input.*`
  reference above) refer to the field link names on the form the workflow
  is attached to — adjust any that don't match yours. A quick way to check:
  add `info input;` right before the `invokeurl` block and look at the
  Deluge execution log after a test record — it dumps every field's actual
  link name and current value.
- Never hardcode `secretKey` in a script you'll share or that other Zoho
  users can view — better practice once you're comfortable with Zoho is to
  store it in Creator's built-in **Secure Preference** store and read it
  back with `zoho.creator.getSecurePreference(...)` instead of a literal
  string. The literal is shown here only to keep the first version simple.
- Test with one manually-created record first and check the Deluge
  execution log (Zoho shows `response` there via the `info` line) — a 401
  there means the secret doesn't match what's set in Render; a 400/500
  means `Ticket_ID`/`Issue_in_Detail` came through empty or Render's
  `OPENROUTER_API_KEY` isn't set.
- This endpoint always classifies with the server's own `OPENROUTER_API_KEY` —
  if a ticket needs a clarifying question, that state (`awaiting_clarification`)
  is stored and visible in your portal's UI same as any manual ticket, but
  nothing prompts anyone automatically; someone still has to open the portal
  to answer it.

The pull-direction Custom API (sections 1–4 above) still has its own,
independent use — on-demand lookup of a specific ticket by id — so both can
coexist; you don't have to pick one.
