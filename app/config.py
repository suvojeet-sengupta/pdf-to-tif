"""Runtime configuration, all overridable through environment variables."""
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


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
