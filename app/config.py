import os
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
