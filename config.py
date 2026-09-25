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

# --- Database ---
DATABASE_URL = _first(
    "DATABASE_URL",
    default=(
        "postgresql://neondb_owner:npg_b6SUnrYcDgj7@"
        "ep-sweet-cake-b4ceoidv-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require"
    ),
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
