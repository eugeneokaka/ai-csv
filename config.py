"""Central config, loaded from api/.env (falling back to real env vars)."""

import os
from pathlib import Path


def _load_env() -> None:
    """Minimal .env loader — real environment variables always win."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env()


def _first(*names: str, default: str = "") -> str:
    """Return the first non-empty env var among names (values are stripped)."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return default


# --- OpenCode ---
OPENCODE_URL = _first("OPENCODE_URL", default="http://127.0.0.1:4096")
MODEL_PROVIDER = _first("MODEL_PROVIDER", default="opencode-go")
MODEL_ID = _first("MODEL_ID", default="mimo-v2.5")

# --- Worker service ---
# The API forwards /chat/ask to this service, which owns the execution pool.
WORKER_URL = _first("WORKER_URL", default="http://127.0.0.1:8001")
WORKER_HOST = _first("WORKER_HOST", default="127.0.0.1")
WORKER_PORT = int(_first("WORKER_PORT", default="8001"))
MAX_WORKERS = int(_first("MAX_WORKERS", default="2"))

# --- Cache eviction ---
# Local `working_dir/` cache is disposable; a chat idle longer than this is
# evicted by the cleanup scheduler. S3 + Postgres stay the source of truth.
CACHE_TTL_HOURS = float(_first("CACHE_TTL_HOURS", default="6"))

# In-worker cleanup scheduler: how often the worker runs the eviction pass.
# Set CLEANUP_ENABLED=false (or interval 0) to disable.
CLEANUP_ENABLED = _first("CLEANUP_ENABLED", default="true").lower() in (
    "1", "true", "yes", "on",
)
CLEANUP_INTERVAL_MINUTES = float(_first("CLEANUP_INTERVAL_MINUTES", default="60"))

# --- Database ---
# Credentials never live in source — set DATABASE_URL in api/.env (or the
# process environment). Fail loudly rather than falling back to a hardcoded URL.
DATABASE_URL = _first("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to api/.env — "
        "credentials must never be hardcoded in source."
    )

# --- AWS / S3 ---
# Accepts both the standard names and the legacy short names.
AWS_ACCESS_KEY_ID = _first(
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY_ID", "Accesskey"
)
AWS_SECRET_ACCESS_KEY = _first(
    "AWS_SECRET_ACCESS_KEY", "Accesssecret"
).rstrip("/")
AWS_REGION = _first("AWS_REGION", default="eu-north-1")
S3_BUCKET = _first("S3_BUCKET")
