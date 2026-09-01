import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Optional fallback only - the UI collects a key from the operator and sends it
# per-request (X-OpenAI-Api-Key header), so nothing secret needs to live in .env.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CLASSIFY_MODEL = os.getenv("OPENAI_CLASSIFY_MODEL", "gpt-4o-mini")
EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Only used by scripts/compare_classifiers.py (OpenAI vs Gemini accuracy
# comparison) - never required for the app's normal classify path, which
# stays OpenAI-only. Few-shot retrieval always uses OpenAI embeddings
# (OPENAI_API_KEY above) even when comparing against Gemini, since that's
# the only embedding backend this app has.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_CLASSIFY_MODEL = os.getenv("GEMINI_CLASSIFY_MODEL", "gemini-2.5-flash")

# When both are set, every OpenAI call (classify, embeddings, resolution
# grading) routes through this Cloudflare AI Gateway instead of hitting
# OpenAI directly - same OpenAI key, same models, but Cloudflare caches
# repeated identical requests and logs usage at gateway.ai.cloudflare.com.
# Reduces how often duplicate calls burn OpenAI's own request quota; does
# NOT raise OpenAI's actual per-account rate limit (that's OpenAI-side,
# billing-gated). Unset means calls go straight to OpenAI as before.
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_AI_GATEWAY_ID = os.getenv("CLOUDFLARE_AI_GATEWAY_ID", "")


def openai_base_url() -> str | None:
    if CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_AI_GATEWAY_ID:
        return f"https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_AI_GATEWAY_ID}/openai"
    return None

CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
MAX_CLARIFICATION_TURNS = int(os.getenv("MAX_CLARIFICATION_TURNS", "2"))
FEWSHOT_K = int(os.getenv("FEWSHOT_K", "5"))

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SQLITE_PATH = DATA_DIR / "tickets.db"
CHROMA_PATH = str(DATA_DIR / "chroma")

# When set (base64-encoded contents of a Firebase service account JSON key),
# app/db.py stores tickets in Firestore instead of the local SQLite file.
# Needed on Render's free plan specifically because its disk is ephemeral -
# every spin-down/redeploy wipes SQLITE_PATH, which would otherwise lose a
# whole test period's worth of classified tickets. Local dev with this unset
# keeps using SQLite exactly as before. Generate the base64 with:
# python -c "import base64; print(base64.b64encode(open('key.json','rb').read()).decode())"
FIREBASE_CREDENTIALS_BASE64 = os.getenv("FIREBASE_CREDENTIALS_BASE64", "")
# Firestore "Database ID" to connect to - defaults to the literal database
# named "(default)", which is what most projects have. Only needs setting if
# your Firestore database was created under a custom Database ID instead.
FIREBASE_DATABASE_ID = os.getenv("FIREBASE_DATABASE_ID", "(default)")

TAXONOMY_PATH = BASE_DIR / "taxonomy.json"

# Single admin account that gates the whole app. Change these in .env for
# anything beyond local/dev use - these defaults are not secure.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# Signs the session cookie. Not a config value - generated once and cached on
# disk (outside git) so logins survive restarts but nothing secret is checked in.
_SESSION_SECRET_PATH = DATA_DIR / ".session_secret"
if _SESSION_SECRET_PATH.exists():
    SESSION_SECRET = _SESSION_SECRET_PATH.read_text().strip()
else:
    SESSION_SECRET = secrets.token_hex(32)
    _SESSION_SECRET_PATH.write_text(SESSION_SECRET)

# --- Zoho Creator integration ---------------------------------------------
# All of this is a SAMPLE/placeholder pending real Zoho details - every value
# is a single env var so going live is an .env edit, never a code change.
#
# ZOHO_INVOKE_URL: must contain the literal "{ticket_id}" placeholder, which
# gets replaced with the requested ticket id before the request is made.
# Only used by the pull-direction lookup (/api/zoho/tickets/{id}, the portal's
# "Look up" box) - the push webhook (/api/webhooks/zoho/tickets) doesn't need
# this at all since Zoho sends ticket data directly. Unset/placeholder until
# the real Zoho Creator Custom API from zoho-invoke-url-setup.md is set up -
# until then, pull-direction lookups will fail with a clear connection/404
# error rather than silently returning fake data.
ZOHO_INVOKE_URL = os.getenv(
    "ZOHO_INVOKE_URL", "https://REPLACE_WITH_REAL_ZOHO_CUSTOM_API/get-ticket?ticket_id={ticket_id}"
)
# Sent as a request header named ZOHO_AUTH_HEADER_NAME with this value.
# Not sure yet whether Zoho expects this as a header at all (vs. baked into
# the URL as the custom-API name) - swap ZOHO_AUTH_HEADER_NAME/ZOHO_API_KEY
# once confirmed, no code change needed either way.
ZOHO_AUTH_HEADER_NAME = os.getenv("ZOHO_AUTH_HEADER_NAME", "Ticket_Classification_version_0")
ZOHO_API_KEY = os.getenv("ZOHO_API_KEY", "sample-zoho-key")

# Field names as they appear (at any depth) in the JSON Zoho returns. These
# are guesses at Zoho's usual "spaces become underscores" convention - see
# zoho-invoke-url-setup.md section 4 for how to confirm/correct these
# against a real response.
ZOHO_FIELD_TICKET_ID = os.getenv("ZOHO_FIELD_TICKET_ID", "Ticket_ID")
ZOHO_FIELD_ISSUE_DETAIL = os.getenv("ZOHO_FIELD_ISSUE_DETAIL", "Issue_in_Detail")

# Shared secret Zoho Creator's Deluge "On Add" workflow sends back as the
# X-Webhook-Secret header when it PUSHes a brand-new ticket to
# POST /api/webhooks/zoho/tickets (see main.py). This is separate from
# ZOHO_API_KEY above (which authenticates *this app* to Zoho when it PULLs a
# ticket) - this one authenticates Zoho to *this app* for the push direction.
# Empty by default so the webhook endpoint is refused (fails closed) until
# you set a real value in .env.
ZOHO_WEBHOOK_SECRET = os.getenv("ZOHO_WEBHOOK_SECRET", "")
