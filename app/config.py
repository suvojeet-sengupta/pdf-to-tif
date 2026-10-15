"""Runtime configuration, all overridable through environment variables."""
import os
import secrets
import tempfile
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
MAX_PAGES = _int("MAX_PAGES", 10000)

# How long a finished job (and its zip) stays on disk before being deleted.
JOB_TTL_MINUTES = _int("JOB_TTL_MINUTES", 120)
CLEANUP_INTERVAL_SECONDS = _int("CLEANUP_INTERVAL_SECONDS", 300)

# How many jobs may render at the same time. One keeps a multi-file batch
# strictly sequential and gives each PDF the whole memory budget.
MAX_CONCURRENT_JOBS = max(1, _int("MAX_CONCURRENT_JOBS", 1))

# Files the UI lets you queue in one go.
MAX_BATCH_FILES = max(1, _int("MAX_BATCH_FILES", 5))

# PDFs a single "convert this folder" job may contain.
MAX_FOLDER_FILES = max(1, _int("MAX_FOLDER_FILES", 1000))

DEFAULT_DPI = _int("DEFAULT_DPI", 200)
MIN_DPI = 72
MAX_DPI = _int("MAX_DPI", 600)

JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Incoming multipart bodies are spooled to a temporary file before the handler
# sees them. Keep that on the data volume: /tmp is a small tmpfs in the
# container, and a few hundred MB there would exhaust it or eat into RAM.
UPLOAD_TMP = DATA_DIR / "tmp"
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(UPLOAD_TMP)
os.environ.setdefault("TMPDIR", str(UPLOAD_TMP))

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

# ---------------------------------------------------------------------------
# Large-document safety rails
# ---------------------------------------------------------------------------
# Pages in flight per worker. Bounding this keeps a 10 000 page job from
# queueing 10 000 pickled tasks at once, and makes cancellation immediate.
INFLIGHT_PER_WORKER = max(1, _int("INFLIGHT_PER_WORKER", 3))

# Abort a job rather than filling the disk. Checked while rendering.
MIN_FREE_DISK_MB = _int("MIN_FREE_DISK_MB", 1024)
DISK_CHECK_EVERY = _int("DISK_CHECK_EVERY", 25)

# A single page that renders to more than this many pixels is scaled down
# instead of exhausting the worker's memory (a 600 dpi A0 sheet is ~1.4 GB).
MAX_PAGE_PIXELS = _int("MAX_PAGE_PIXELS", 120_000_000)


def _container_memory_limit() -> int:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 50:
            return int(raw)
    return 0


# Estimated bytes all render workers together may hold at once. Pages are only
# dispatched while they fit, so a batch of huge sheets renders one or two at a
# time instead of all workers allocating together and tripping the OOM killer.
_limit = _container_memory_limit()
RENDER_MEMORY_BYTES = _int(
    "RENDER_MEMORY_MB", int(_limit * 0.8) // (1024 * 1024) if _limit else 2048
) * 1024 * 1024

# ---------------------------------------------------------------------------
# Chunked uploads
# ---------------------------------------------------------------------------
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOAD_CHUNK_BYTES = _int("UPLOAD_CHUNK_MB", 4) * 1024 * 1024
# An unfinished upload is discarded after this long without activity.
UPLOAD_TTL_MINUTES = _int("UPLOAD_TTL_MINUTES", 180)

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
