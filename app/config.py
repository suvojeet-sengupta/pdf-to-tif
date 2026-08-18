"""Runtime configuration, all overridable through environment variables."""
import os
import secrets
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"

PORT = _int("PORT", 9000)

# Number of worker processes used to render pages in parallel.
RENDER_WORKERS = max(1, _int("RENDER_WORKERS", min(8, (os.cpu_count() or 2))))

# Hard limits so a single upload cannot exhaust the VPS.
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 500)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_PAGES = _int("MAX_PAGES", 3000)

# How long a finished job (and its zip) stays on disk before being deleted.
JOB_TTL_MINUTES = _int("JOB_TTL_MINUTES", 120)
CLEANUP_INTERVAL_SECONDS = _int("CLEANUP_INTERVAL_SECONDS", 300)

# How many jobs may render at the same time.
MAX_CONCURRENT_JOBS = max(1, _int("MAX_CONCURRENT_JOBS", 2))

DEFAULT_DPI = _int("DEFAULT_DPI", 200)
MIN_DPI = 72
MAX_DPI = _int("MAX_DPI", 600)

JOBS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
# A single shared password gates the whole app. Leave APP_PASSWORD empty only
# if the instance is already protected by something else (Cloudflare Access,
# a VPN, ...) - then set ALLOW_NO_PASSWORD=true to acknowledge it.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
ALLOW_NO_PASSWORD = _bool("ALLOW_NO_PASSWORD", False)
AUTH_ENABLED = bool(APP_PASSWORD)

# Signs the session cookie. Set it so sessions survive a container restart;
# a generated one logs everybody out on every restart.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip() or secrets.token_hex(32)
SESSION_TTL_HOURS = _int("SESSION_TTL_HOURS", 24 * 14)
SESSION_COOKIE = "p2t_session"
# Send the cookie only over HTTPS. Turn off if you browse over plain http://.
COOKIE_SECURE = _bool("COOKIE_SECURE", True)

# Brute-force guard for the login form.
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 8)
LOGIN_LOCKOUT_SECONDS = _int("LOGIN_LOCKOUT_SECONDS", 300)

# How many finished jobs stay listed in the library.
LIBRARY_LIMIT = _int("LIBRARY_LIMIT", 40)

APP_NAME = os.environ.get("APP_NAME", "Pagesmith").strip() or "Pagesmith"
