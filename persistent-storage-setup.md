# Persistent ticket storage (Turso)

## Why this exists

Render's free web service plan uses **ephemeral disk** - every time the
service spins down from inactivity and wakes back up (or you redeploy), the
filesystem resets to whatever's in the deployed image. `app/data/tickets.db`
(the SQLite file every classified ticket gets written to) lives on that
disk, so a spin-down doesn't just cause a slow wake-up, it silently erases
ticket history the next time someone looks.

`app/db.py` supports a `TURSO_DATABASE_URL` environment variable - when set,
it stores tickets/turns/corrections in Turso (a remote libSQL database -
SQLite's own wire protocol and SQL dialect, just hosted) instead of local
SQLite, so history survives restarts. When unset (e.g. local dev), nothing
changes - it keeps using SQLite exactly as before. Because Turso speaks the
same SQL as SQLite, `app/db.py` runs the exact same queries against either
backend; only the connection differs.

## 1. Create a Turso database

Install the Turso CLI and create a database (interactive login opens a
browser - you'll need to do this step yourself):

```bash
curl -sSfL https://get.tur.so/install.sh | bash
turso auth login
turso db create ticket-classifier
turso db show ticket-classifier --url          # -> TURSO_DATABASE_URL
turso db tokens create ticket-classifier       # -> TURSO_AUTH_TOKEN
```

(Or use the [Turso dashboard](https://app.turso.tech) instead of the CLI -
same two values either way: a `libsql://...` database URL and an auth
token.)

## 2. Point Render at it

In your Render service's **Environment** tab, add:

```
TURSO_DATABASE_URL=libsql://your-database-name.turso.io
TURSO_AUTH_TOKEN=<the token from `turso db tokens create`>
```

Save - Render redeploys automatically. `db.init_db()` creates the schema in
Turso on first connect if it doesn't already exist, same as it does for a
fresh local SQLite file - no separate migration step needed for a new
database.

## 3. Verify

After redeploying, push a test ticket through (see
`zoho-invoke-url-setup.md` section 5) and then check `/api/tickets` in the
portal, or query the database directly with `turso db shell
ticket-classifier "SELECT COUNT(*) FROM tickets"`.

## Migrating existing data from Firestore

If you're moving off an existing Firestore setup rather than starting
fresh, `scripts/migrate_firestore_to_turso.py` copies every ticket (+ its
turns/corrections) across, preserving ids and timestamps exactly - read-only
against Firestore, safely re-runnable if interrupted:

```bash
pip install firebase-admin   # not a normal app dependency - only needed for this one-off script
FIREBASE_CREDENTIALS_BASE64=... TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... \
  python scripts/migrate_firestore_to_turso.py
```

It prints a Firestore-vs-Turso count comparison at the end. Once you've
confirmed the counts match and the app works correctly against Turso, you
can remove `FIREBASE_CREDENTIALS_BASE64`/`FIREBASE_DATABASE_ID` from
Render's environment and decommission the Firebase project whenever you're
ready - nothing in this script does that automatically.

## Importing historical data (new tickets, not a Firestore migration)

`scripts/import_zoho_csv.py` bulk-imports a Zoho "Instructors Ticketing
System" CSV export, classifying each row through the same pipeline a live
webhook ticket uses:

```bash
TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... OPENROUTER_API_KEY=sk-or-... OPENAI_API_KEY=sk-... \
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
