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

CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
MAX_CLARIFICATION_TURNS = int(os.getenv("MAX_CLARIFICATION_TURNS", "2"))
FEWSHOT_K = int(os.getenv("FEWSHOT_K", "5"))

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SQLITE_PATH = DATA_DIR / "tickets.db"
CHROMA_PATH = str(DATA_DIR / "chroma")

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
