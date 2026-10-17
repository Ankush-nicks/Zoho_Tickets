import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str = "") -> str:
    """os.getenv, stripped - a dashboard copy-paste (Render, Replit, etc.)
    very easily leaves a trailing newline/space on a pasted value, which
    silently breaks anything that puts it straight into an HTTP header
    (http.client raises ValueError: Invalid header value on a literal \\n)."""
    return os.getenv(name, default).strip()

# Classification goes through OpenRouter now, not
# OpenAI directly - one key with access to many models/providers and its own
# (usually more forgiving) rate limits, instead of being pinned to a single
# OpenAI key's per-account limit. Model id needs OpenRouter's "provider/model"
# form (see https://openrouter.ai/models); the default keeps the same
# underlying OpenAI model as before, just routed through OpenRouter, so the
# strict json_schema structured-output path needs no changes.
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_CLASSIFY_MODEL = _env("OPENROUTER_CLASSIFY_MODEL", "openai/gpt-4o-mini")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# OpenAI is kept ONLY for embeddings now (memory.py's few-shot retrieval) -
# OpenRouter has no embeddings endpoint of its own. This key is never used
# for classification anymore.
OPENAI_API_KEY = _env("OPENAI_API_KEY")
EMBED_MODEL = _env("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Only used by scripts/compare_classifiers.py (OpenAI vs Gemini accuracy
# comparison) - never required for the app's normal classify path. Few-shot
# retrieval always uses OpenAI embeddings (OPENAI_API_KEY above) even when
# comparing against Gemini, since that's the only embedding backend this
# app has.
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_CLASSIFY_MODEL = _env("GEMINI_CLASSIFY_MODEL", "gemini-2.5-flash")

# When both are set, OpenAI embedding calls (memory.py) route through this
# Cloudflare AI Gateway instead of hitting OpenAI directly - same OpenAI key,
# same model, but Cloudflare caches repeated identical requests and logs
# usage at gateway.ai.cloudflare.com. Does NOT apply to classify()
# anymore now that it calls OpenRouter, not OpenAI.
CLOUDFLARE_ACCOUNT_ID = _env("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_AI_GATEWAY_ID = _env("CLOUDFLARE_AI_GATEWAY_ID")

# Automatic classify() fallback when OpenRouter hits a
# rate limit - a real Cloudflare API token with "Workers AI" permission
# (separate from AI Gateway's own permission group above). Few-shot
# retrieval still goes through OpenAI embeddings first (unaffected by
# OpenRouter's own limits) - only the final classification call
# itself moves to Cloudflare. Unset means these calls raise on rate limit
# same as before.
CLOUDFLARE_API_TOKEN = _env("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_WORKERS_AI_MODEL = _env("CLOUDFLARE_WORKERS_AI_MODEL", "@cf/meta/llama-3.1-8b-instruct")


def openai_base_url() -> str | None:
    """Base URL for direct OpenAI calls - embeddings only now (see above)."""
    if CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AI_GATEWAY_ID:
        return f"https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_AI_GATEWAY_ID}/openai"
    return None

CONFIDENCE_THRESHOLD = float(_env("CONFIDENCE_THRESHOLD", "0.65"))
MAX_CLARIFICATION_TURNS = int(_env("MAX_CLARIFICATION_TURNS", "2"))
FEWSHOT_K = int(_env("FEWSHOT_K", "5"))
# Cosine similarity floor (1 - chroma's cosine distance, range roughly -1..1)
# below which a retrieved memory example is dropped rather than forced into
# the few-shot prompt. Without this, a genuinely novel ticket still gets k
# "closest but irrelevant" examples, which can bias the model toward a
# wrong-but-similar-sounding category. 0.15 is a conservative starting
# floor, not a calibrated value - there's no hand-labeled eval set backing
# it (same caveat as CONFIDENCE_THRESHOLD). Tune it by looking at real
# `similarity` values retrieve_similar() returns for known-good vs.
# known-bad matches once enough correction history exists.
FEWSHOT_MIN_SIMILARITY = float(_env("FEWSHOT_MIN_SIMILARITY", "0.15"))
# Default number of extra examples memory.retrieve_similar adds when asked
# for also_from_categories. The classifier no longer asks (it never sees the
# instructor's pick), so this only matters to other callers.
FEWSHOT_REPORTER_K = int(_env("FEWSHOT_REPORTER_K", "2"))
# How sure the model must be before it overrides the subcategory the
# instructor picked on the Zoho form - see classifier.apply_reporter_prior.
# Below this, a disagreement keeps the instructor's pick: overwriting it on
# a weak hunch was the main source of misrouting (short/vague issue text,
# where the reporter's own pick carries intent the text doesn't). Not a
# calibrated value - tune it once enough overrides have been reviewed.
REPORTER_OVERRIDE_MIN_CONFIDENCE = float(_env("REPORTER_OVERRIDE_MIN_CONFIDENCE", "0.9"))
# Tickets that arrived in this app more than this many days ago are deleted,
# with everything stored for them (see main._cleanup_old_tickets) - keeps the
# free-tier database small. The taxonomy is never deleted.
TICKET_RETENTION_DAYS = int(_env("TICKET_RETENTION_DAYS", "60"))

DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SQLITE_PATH = DATA_DIR / "tickets.db"
CHROMA_PATH = str(DATA_DIR / "chroma")

# When set, app/db.py stores tickets in Turso (a remote libSQL/SQLite-
# compatible database) instead of the local SQLite file. Needed on Render's
# free plan specifically because its disk is ephemeral - every spin-down/
# redeploy wipes SQLITE_PATH, which would otherwise lose a whole test
# period's worth of classified tickets. Local dev with this unset keeps
# using SQLite exactly as before. Create a database and token with the
# Turso CLI (`turso db create ...`, `turso db tokens create ...`) or the
# Turso dashboard - see persistent-storage-setup.md.
TURSO_DATABASE_URL = _env("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = _env("TURSO_AUTH_TOKEN")

TAXONOMY_PATH = BASE_DIR / "taxonomy.json"

# Single admin account that gates the whole app. Change these in .env for
# anything beyond local/dev use - these defaults are not secure.
ADMIN_USERNAME = _env("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "admin")

# Second gate in front of the Taxonomy tab specifically - everyone logged
# into the app can view it, but editing/saving needs this password too
# (checked both at the "Unlock to edit" prompt and again on every PUT
# /api/taxonomy, so the second check can't be skipped by calling the API
# directly). Set in .env for anything beyond local/dev use.
TAXONOMY_EDIT_PASSWORD = _env("TAXONOMY_EDIT_PASSWORD", "changeme")

# Signs the session cookie. Not a config value - generated once and cached on
# disk (outside git) so logins survive restarts but nothing secret is checked in.
_SESSION_SECRET_PATH = DATA_DIR / ".session_secret"
if _SESSION_SECRET_PATH.exists():
    SESSION_SECRET = _SESSION_SECRET_PATH.read_text().strip()
else:
    SESSION_SECRET = secrets.token_hex(32)
    _SESSION_SECRET_PATH.write_text(SESSION_SECRET)

# Shared secret Zoho Creator's Deluge "On Add" workflow sends back as the
# X-Webhook-Secret header when it PUSHes a brand-new ticket to
# POST /api/webhooks/zoho/tickets (see main.py) - authenticates Zoho to
# *this app*.
# Empty by default so the webhook endpoint is refused (fails closed) until
# you set a real value in .env.
ZOHO_WEBHOOK_SECRET = _env("ZOHO_WEBHOOK_SECRET")

# category_of_the_issue/sub_category_of_the_issue are mandatory fields on the
# Zoho side, so the webhook must never write them back as blank. This is the
# taxonomy leaf id it falls back to when the real predicted/stored
# category_id can't be resolved (never classified yet, classify() itself
# failed, or the id was orphaned by a later Taxonomy tab edit - see
# taxonomy.py's reload() note) - "G14-S01" is the taxonomy's own catch-all
# "Insufficient Information / No Routing Required" leaf, already what the
# classifier's own prompt rules use for a genuinely un-classifiable ticket.
ZOHO_FALLBACK_CATEGORY_ID = _env("ZOHO_FALLBACK_CATEGORY_ID", "G14-S01")
