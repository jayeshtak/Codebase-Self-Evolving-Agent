import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "memory.db"

# ── API Keys ──────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# ── Models ────────────────────────────────────────────────────────────────────

GEMINI_FLASH = "gemini-2.5-flash"       # bulk scanning, planning, test writing
GEMINI_PRO = "gemini-2.5-flash"         # fix generation (use flash if pro quota hit)
GROQ_MODEL = "llama-3.3-70b-versatile"  # Groq fallback model

# ── Agent settings ────────────────────────────────────────────────────────────

MAX_REVIEW_ITERATIONS = 3
MAX_HEAL_ATTEMPTS = 2
DEFAULT_SCAN_HOUR = 2        # 2am daily scan
COMPLEXITY_THRESHOLD = 10
FUNCTION_LENGTH_THRESHOLD = 60
NESTING_DEPTH_THRESHOLD = 4

# ── Server & Deployment settings ──────────────────────────────────────────────

PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in ("true", "1", "yes")

# ── Validate ──────────────────────────────────────────────────────────────────

def has_gemini() -> bool:
    return bool(GEMINI_API_KEY and GEMINI_API_KEY.strip())

def has_groq() -> bool:
    return bool(GROQ_API_KEY and GROQ_API_KEY.strip())

def validate(require_keys: bool = False):
    """
    Validates agent settings and ensures data directory exists.
    If require_keys is True, raises EnvironmentError if neither key is set.
    """
    # Ensure the data directory exists, as DB_PATH depends on it
    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[Config] Created data directory: {DATA_DIR}")

    # Validate agent settings
    if not isinstance(MAX_REVIEW_ITERATIONS, int) or MAX_REVIEW_ITERATIONS <= 0:
        raise ValueError(
            f"Invalid MAX_REVIEW_ITERATIONS: {MAX_REVIEW_ITERATIONS}. "
            "Must be a positive integer."
        )
    
    if not isinstance(DEFAULT_SCAN_HOUR, int) or not (0 <= DEFAULT_SCAN_HOUR < 24):
        raise ValueError(
            f"Invalid DEFAULT_SCAN_HOUR: {DEFAULT_SCAN_HOUR}. "
            "Must be an integer between 0 and 23."
        )

    if not isinstance(COMPLEXITY_THRESHOLD, int) or COMPLEXITY_THRESHOLD <= 0:
        raise ValueError(
            f"Invalid COMPLEXITY_THRESHOLD: {COMPLEXITY_THRESHOLD}. "
            "Must be a positive integer."
        )

    if not isinstance(FUNCTION_LENGTH_THRESHOLD, int) or FUNCTION_LENGTH_THRESHOLD <= 0:
        raise ValueError(
            f"Invalid FUNCTION_LENGTH_THRESHOLD: {FUNCTION_LENGTH_THRESHOLD}. "
            "Must be a positive integer."
        )

    if not isinstance(NESTING_DEPTH_THRESHOLD, int) or NESTING_DEPTH_THRESHOLD <= 0:
        raise ValueError(
            f"Invalid NESTING_DEPTH_THRESHOLD: {NESTING_DEPTH_THRESHOLD}. "
            "Must be a positive integer."
        )

    missing = []
    if not has_gemini():
        missing.append("GEMINI_API_KEY")
    if not has_groq():
        missing.append("GROQ_API_KEY")

    if missing:
        msg = f"[Config] Notice: Optional LLM keys missing ({', '.join(missing)}). Running in degraded/static analysis mode for missing providers."
        if require_keys and len(missing) == 2:
            raise EnvironmentError(
                f"Missing all LLM API keys: {missing}. "
                f"Add at least one to a .env file in the project root."
            )
        else:
            print(msg)

if __name__ == "__main__":
    try:
        validate()
        print("[Config] All settings are valid.")
        print(f"[Config] Gemini available: {has_gemini()}, Groq available: {has_groq()}")
        print(f"[Config] Database path: {DB_PATH}")
    except (EnvironmentError, ValueError) as e:
        print(f"[Config] {e}")