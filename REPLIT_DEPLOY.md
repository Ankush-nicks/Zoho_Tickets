# Deploying to Replit (Reserved VM)

## Why move off Render

Render's free plan has two problems for a live webhook receiver: it spins the
service down after inactivity (the *next* request pays a cold-start delay -
bad for a Zoho "On Add" push that expects a fast response), and its disk is
ephemeral (wiped on every spin-down/redeploy, which is the entire reason
Firestore exists in this app - see `persistent-storage-setup.md`).

A Replit **Reserved VM** deployment runs on a dedicated VM that never sleeps
- no cold starts, and it's the correct fit for this app specifically because
it has two `asyncio` background loops (`_auto_classify_loop`,
`_auto_score_loop` in `app/main.py`) that need to keep running between
requests. Replit's **Autoscale** deployment type would kill those loops
every time it scales to zero, so don't use it for this app.

## What this migration does and doesn't change

- **Storage stays on Firestore.** Firestore already survives redeploys/
  restarts regardless of which host runs the app, so there's no reason to
  migrate off it just because the host changed. What *did* need fixing was
  `app/db.py` and `app/main.py` calling `list_all_tickets()` (a full
  collection read - one Firestore read per document ever stored) from two
  background loops every 30 minutes regardless of traffic, plus several
  page-load endpoints. That's now `list_pending_tickets()` /
  `count_pending_tickets()` / `list_tickets_by_raw_status()` instead - all
  server-side filtered queries whose read cost scales with how many tickets
  are actually pending/closed, not with total ticket history. That's the
  actual fix for the free-tier quota being hit; moving host is separate.
- **Not fixed by this**: the Chroma vector store (`app/data/chroma`) still
  lives on local disk and was already flagged in `persistent-storage-setup.md`
  as not covered by the Firestore migration - it resets on Render
  spin-down today, and would reset on a Replit redeploy too if you don't
  keep it in mind. This only affects how much dynamic few-shot context
  corrections contribute over time, not ticket history or correctness -
  worth revisiting later, not urgent.

## Steps

1. **Import the repo.** In Replit: Create App -> Import from GitHub (or
   upload this folder directly).
2. **Add Secrets** (Replit's equivalent of Render's Environment tab - the
   padlock icon in the workspace sidebar, or the Deploy dialog's own Secrets
   section):
   - `OPENROUTER_API_KEY` (classification/resolution grading)
   - `OPENAI_API_KEY` (embeddings only - few-shot memory retrieval)
   - `FIREBASE_CREDENTIALS_BASE64` (same base64 string you're using on
     Render today - see `persistent-storage-setup.md` step 2 if you need to
     regenerate it)
   - `FIREBASE_DATABASE_ID` only if you use a non-default Firestore database
   - `ADMIN_USERNAME` / `ADMIN_PASSWORD`
   - `ZOHO_WEBHOOK_SECRET`, `ZOHO_INVOKE_URL`, `ZOHO_API_KEY` etc. from your
     current Render environment - copy every var Render has today
3. **Verify the workspace preview works first.** Hit the Run button - `.replit`
   is already configured to run `uvicorn app.main:app --host 0.0.0.0 --port
   8000`. Log in and push a test ticket through before deploying.
4. **Deploy -> Reserved VM.** In the Deploy dialog, pick **Reserved VM**
   (not Autoscale - see above), the smallest machine size (1 vCPU / 4 GiB is
   plenty for this app's traffic), and confirm the build/run commands match
   `.replit`'s `[deployment]` block. This is also where the actual
   `deploymentTarget` gets written into `.replit` - that's deliberate, see
   the comment at the top of that file.
5. **Point Zoho at the new URL.** Once deployed, Replit gives you a stable
   `https://<your-app>.replit.app` URL - update `ZOHO_INVOKE_URL` (if used)
   and the webhook URL configured in Zoho Creator's "On Add" workflow to
   point here instead of the old Render URL.
6. **Retire the Render service** once you've confirmed a real ticket flows
   through end-to-end on Replit.

## Cost

Reserved VM at 1 vCPU / 4 GiB runs ~$0.0486/hour - roughly **$35/month** for
24/7 uptime. Your Core plan's $20/month credit applies to deployment
compute, so the net additional spend beyond what you're already paying
Replit is roughly **$15-18/month**. Storage (SQLite is gone now that
Firestore holds tickets; Chroma is well under 1 GB) and egress add pennies.
