# Persistent ticket storage (Firestore)

## Why this exists

Render's free web service plan uses **ephemeral disk** - every time the
service spins down from inactivity and wakes back up (or you redeploy), the
filesystem resets to whatever's in the deployed image. `app/data/tickets.db`
(the SQLite file every classified ticket gets written to) lives on that
disk, so a spin-down doesn't just cause a slow wake-up, it silently erases
ticket history the next time someone looks.

`app/db.py` supports a `FIREBASE_CREDENTIALS_BASE64` environment variable -
when set, it stores tickets/turns/corrections in Firestore instead of local
SQLite, so history survives restarts. When unset (e.g. local dev), nothing
changes - it keeps using SQLite exactly as before.

## 1. Create a Firestore database

1. Go to the [Firebase Console](https://console.firebase.google.com), create
   a project (or use an existing one).
2. **Build → Firestore Database → Create database**. This is a separate step
   from just creating the project - the database doesn't exist until you
   click through this. Choose **Native mode** and any nearby location.
   - If you leave the **Database ID** field as the default, it's created as
     literally `(default)`, which the app assumes unless told otherwise.
     If you type a custom Database ID instead, you'll need to set
     `FIREBASE_DATABASE_ID` to match (see below) - the SDK only connects to
     `(default)` unless told otherwise, and will fail with `"The database
     (default) does not exist"` if you skip this.
3. **Project Settings (gear icon) → Service Accounts → Generate new private
   key** - downloads a JSON file. This is a real credential (broader access
   than just Firestore, depending on the service account's role) - don't
   commit it to git (the repo's `.gitignore` already excludes
   `*firebase-adminsdk*.json` as a safety net, but keep it out of the repo
   directory entirely if you can).

## 2. Point Render at it

Base64-encode the downloaded key (avoids newline-escaping issues a raw
multi-line JSON paste would hit in an env var UI):

```bash
python -c "import base64; print(base64.b64encode(open('key.json','rb').read()).decode())"
```

In your Render service's **Environment** tab, add:

```
FIREBASE_CREDENTIALS_BASE64=<the base64 string>
```

Only add `FIREBASE_DATABASE_ID=<your-database-id>` if you used a custom
Database ID in step 1 - omit it entirely to use `(default)`.

Save - Render redeploys automatically. Firestore is schemaless, so there's
no migration step; `db.init_db()` just verifies the connection at startup.

## 3. Verify

After redeploying, push a test ticket through (see
`zoho-invoke-url-setup.md` section 5) and then check `/api/tickets` in the
portal, or look at the `tickets` collection directly in the Firebase
Console's Firestore Data tab.

## Importing historical data

`scripts/import_zoho_csv.py` bulk-imports a Zoho "Instructors Ticketing
System" CSV export, classifying each row through the same pipeline a live
webhook ticket uses:

```bash
FIREBASE_CREDENTIALS_BASE64=... OPENAI_API_KEY=sk-... \
  python scripts/import_zoho_csv.py "path/to/export.csv"
```

Safely re-runnable - rows whose Ticket ID already exists are skipped rather
than re-classified, so an interrupted run can just be started again. See the
script's docstring for the CSV column mapping.

## What this does and doesn't fix

- **Fixes:** ticket/classification/correction history now survives
  spin-downs and redeploys.
- **Doesn't fix:** cold-start latency. The free plan still spins the
  service down after inactivity, so the *first* request after a quiet
  period (e.g. a Zoho "On Add" push) will be slow while it wakes up. If a
  ticket seems to have not gone through, check the portal before assuming
  it failed - it likely just landed during a cold start. If missed/slow
  pushes become a real problem, revisit adding a keep-alive ping (e.g. a
  free https://cron-job.org check every ~10 min) or upgrading to a paid
  Render instance.
- **Not covered here:** the Chroma vector store (`app/data/chroma`, used
  for few-shot example retrieval) still lives on local ephemeral disk and
  will reset on spin-down/redeploy the same way SQLite used to. This only
  affects how much dynamic few-shot context corrections contribute over
  time, not correctness or ticket history - worth revisiting later if
  correction-driven accuracy improvement matters long-term.
