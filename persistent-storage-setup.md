# Persistent ticket storage for the week-long accuracy test

## Why this exists

Render's free web service plan uses **ephemeral disk** - every time the
service spins down from inactivity and wakes back up (or you redeploy), the
filesystem resets to whatever's in the deployed image. `app/data/tickets.db`
(the SQLite file every classified ticket gets written to) lives on that
disk, so a spin-down doesn't just cause a slow wake-up, it silently erases
the whole test period's history the next time someone looks.

`app/db.py` now supports a `DATABASE_URL` environment variable - when set,
it stores tickets in that Postgres database instead of local SQLite, so
history survives restarts. When unset (e.g. local dev), nothing changes -
it keeps using SQLite exactly as before.

## 1. Create a free Postgres database (Neon)

1. Go to [neon.tech](https://neon.tech) and sign up (free tier - no card
   required for the base plan).
2. Create a new project. Any region is fine.
3. On the project dashboard, copy the **connection string** shown - it
   looks like:
   ```
   postgresql://<user>:<password>@<host>/<dbname>?sslmode=require
   ```
   (Supabase works the same way if you'd rather use that instead - its
   dashboard has an equivalent "Connection string" panel under
   Project Settings → Database.)

## 2. Point Render at it

In your Render service's **Environment** tab, add:

```
DATABASE_URL=postgresql://<user>:<password>@<host>/<dbname>?sslmode=require
```

Save - Render redeploys automatically. On that next startup, `db.init_db()`
creates the `tickets` / `turns` / `corrections` tables in the Postgres
database automatically (same as it does for a fresh local SQLite file), so
there's no manual schema step.

## 3. Verify

After redeploying, push a test ticket through (see
`zoho-invoke-url-setup.md` section 5) and then check `/api/tickets` in the
portal, or query the Neon dashboard's SQL console:

```sql
select id, zoho_ticket_id, status, category_id, confidence, created_at
from tickets order by created_at desc;
```

Rows showing up there (not just in the portal UI) confirm it's actually
Postgres-backed, not the local ephemeral file.

## What this does and doesn't fix

- **Fixes:** ticket/classification/correction history now survives
  spin-downs and redeploys - safe to run the week-long accuracy test.
- **Doesn't fix:** cold-start latency. The free plan still spins the
  service down after inactivity, so the *first* request after a quiet
  period (e.g. a Zoho "On Add" push) will be slow while it wakes up. If a
  ticket seems to have not gone through, check the portal before assuming
  it failed - it likely just landed during a cold start. If missed/slow
  pushes become a real problem during the test, revisit adding a keep-alive
  ping (e.g. a free https://cron-job.org check every ~10 min) or upgrading
  to a paid Render instance.
- **Not covered here:** the Chroma vector store (`app/data/chroma`, used
  for few-shot example retrieval) still lives on local ephemeral disk and
  will reset on spin-down/redeploy the same way SQLite used to. This only
  affects how much dynamic few-shot context corrections contribute over
  time, not correctness or ticket history - left out of scope here since it
  wasn't blocking this week's test, but worth revisiting later if
  correction-driven accuracy improvement matters long-term.
