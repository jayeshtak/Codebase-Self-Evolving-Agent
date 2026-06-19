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

# ── Validate ──────────────────────────────────────────────────────────────────

def validate():
    """
    Validates that required API keys are present, ensures the data directory exists,
    and checks critical agent settings.
    """
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    
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

    if missing:
        raise EnvironmentError(
            f"Missing API keys: {missing}. "
            f"Add them to a .env file in the project root."
        )

if __name__ == "__main__":
    try:
        validate()
        print("[Config] All API keys and settings are valid.")
        print(f"[Config] Database path: {DB_PATH}")
    except (EnvironmentError, ValueError) as e:
        print(f"[Config] {e}")