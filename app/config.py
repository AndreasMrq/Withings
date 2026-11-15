"""Runtime configuration, read from environment variables (see .env.example)."""
import os
import secrets
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = Path(_env("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(_env("DB_PATH", str(DATA_DIR / "health.db")))

# Withings developer app credentials (https://developer.withings.com/dashboard/)
WITHINGS_CLIENT_ID = _env("WITHINGS_CLIENT_ID")
WITHINGS_CLIENT_SECRET = _env("WITHINGS_CLIENT_SECRET")
# Public base URL of this app, e.g. https://health.example.com  (no trailing slash)
PUBLIC_URL = _env("PUBLIC_URL", "http://localhost:8000").rstrip("/")
WITHINGS_REDIRECT_URI = _env("WITHINGS_REDIRECT_URI", f"{PUBLIC_URL}/api/withings/callback")
# Register Withings push notifications (needs PUBLIC_URL reachable from the internet)
WITHINGS_NOTIFY = _env("WITHINGS_NOTIFY", "false").lower() in ("1", "true", "yes")

# Login password for the web UI. Leave empty to disable auth (only on a trusted LAN!).
APP_PASSWORD = _env("APP_PASSWORD")


def _secret_key() -> str:
    key = _env("SECRET_KEY")
    if key:
        return key
    f = DATA_DIR / ".secret_key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


SECRET_KEY = _secret_key()

SYNC_INTERVAL_MIN = int(_env("SYNC_INTERVAL_MIN", "30"))
HISTORY_DAYS = int(_env("HISTORY_DAYS", "90"))
DEFAULT_TZ = _env("TZ", "UTC") or "UTC"
