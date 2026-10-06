"""Cache eviction runner.

The local `working_dir/` is a disposable cache: uploads (and every AI output)
are backed by S3 + a `document` row, and Postgres holds all metadata. This
runner evicts the local cache of chats idle longer than the TTL, without ever
touching S3, `document`, `chat` or `prompt`.

Safety:
  * chats with a running or queued ask (worker `/active`) are never evicted;
  * a local file is deleted only when a `ready` document backs it (S3 copy);
    un-backed files are left in place and logged as orphans;
  * stray files outside chat folders (including the legacy `api/output/`) older
    than the TTL are removed.

Run once from cron / a systemd timer / Task Scheduler:

    python cleanup.py [--ttl-hours 6]
"""

import argparse
import logging
import shutil
import time
from pathlib import Path

import httpx

import documents
from config import CACHE_TTL_HOURS, WORKER_URL
from db import get_conn

logger = logging.getLogger("cleanup")

API_DIR = Path(__file__).resolve().parent
WORKDIR = API_DIR / "working_dir"
UPLOADS_ROOT = WORKDIR / "uploads"
OUTPUT_ROOT = WORKDIR / "output"
LEGACY_OUTPUT = API_DIR / "output"


def _active_chats() -> set[str] | None:
    """Chats with work in flight, or None if the worker can't be reached."""
    try:
        r = httpx.get(f"{WORKER_URL}/active", timeout=3)
        r.raise_for_status()
        return set(r.json().get("chats", []))
    except Exception as exc:
        logger.warning("Worker /active unreachable (%s)", exc)
        return None


def _idle_chats(ttl_hours: float) -> list[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM chat WHERE updated_at < NOW() - make_interval(secs => %s)",
        (int(ttl_hours * 3600),),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r["id"] for r in rows]


def _backed_filenames(chat_id: str) -> dict[str, set[str]]:
    """Filenames per source that have a `ready` S3 document."""
    backed: dict[str, set[str]] = {documents.UPLOAD: set(), documents.OUTPUT: set()}
    for doc in documents.list_documents(chat_id):
        if doc["status"] == "ready" and doc.get("s3_key"):
            backed.setdefault(doc["source"], set()).add(doc["filename"])
    return backed


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _rmdir_if_empty(path: Path) -> None:
    if path.is_dir():
        try:
            if not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass


def _evict_chat(chat_id: str, backed: dict[str, set[str]],
                dry_run: bool = False) -> tuple[int, int]:
    """Delete backed files + the run dir for one idle chat. Returns (freed, orphans)."""
    freed = 0
    orphans = 0
    for root, source in (
        (UPLOADS_ROOT / chat_id, documents.UPLOAD),
        (OUTPUT_ROOT / chat_id, documents.OUTPUT),
    ):
        if not root.is_dir():
            continue
        for f in root.iterdir():
            if not f.is_file():
                continue
            if f.name in backed.get(source, set()):
                freed += f.stat().st_size
                if dry_run:
                    logger.info("[dry-run] would delete backed file: %s", f)
                else:
                    f.unlink()
            else:
                orphans += 1
                logger.info("Keeping un-backed local file: %s", f)
    run_dir = WORKDIR / chat_id
    if run_dir.is_dir():
        freed += _dir_size(run_dir)
        if dry_run:
            logger.info("[dry-run] would delete run dir: %s", run_dir)
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
    if not dry_run:
        for d in (UPLOADS_ROOT / chat_id, OUTPUT_ROOT / chat_id):
            _rmdir_if_empty(d)
    return freed, orphans


def _sweep_empty_chat_dirs(active: set[str], dry_run: bool = False) -> int:
    """Remove empty chat dirs (uploads/output/run) for non-active chats."""
    removed = 0
    for root in (UPLOADS_ROOT, OUTPUT_ROOT):
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir() and d.name not in active and not any(d.iterdir()):
                if dry_run:
                    logger.info("[dry-run] would remove empty dir: %s", d)
                else:
                    d.rmdir()
                removed += 1
    if WORKDIR.is_dir():
        for d in WORKDIR.iterdir():
            if (
                d.is_dir()
                and d.name not in active
                and d.name not in ("uploads", "output")
                and not any(d.iterdir())
            ):
                if dry_run:
                    logger.info("[dry-run] would remove empty dir: %s", d)
                else:
                    d.rmdir()
                removed += 1
    return removed


def _sweep_stray_files(ttl_seconds: float, dry_run: bool = False) -> int:
    """Remove stray files sitting directly in the cache/legacy roots."""
    cutoff = time.time() - ttl_seconds
    removed = 0
    for root in (LEGACY_OUTPUT, OUTPUT_ROOT, UPLOADS_ROOT):
        if not root.is_dir():
            continue
        for f in root.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                if dry_run:
                    logger.info("[dry-run] would remove stray file: %s", f)
                else:
                    logger.info("Removing stray file: %s", f)
                    f.unlink()
                removed += 1
    return removed


def _still_idle(chat_id: str, ttl_seconds: int) -> bool:
    """Re-check at eviction time — the chat may have been used since the query."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT 1 AS ok FROM chat
            WHERE id = %s AND updated_at < NOW() - make_interval(secs => %s)""",
        (chat_id, ttl_seconds),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def run_once(ttl_hours: float = CACHE_TTL_HOURS, dry_run: bool = False,
             active_chats: set[str] | None = None, active_provider=None) -> dict:
    """Evict idle chat caches once.

    Busy chats are never evicted. Two in-process callers supply that set:
    `active_chats` (a fixed set) or `active_provider` (a callable returning the
    live set, re-read per chat to shrink the race window). With neither, the
    worker is queried over HTTP `/active`.
    """
    started = time.monotonic()
    if callable(active_provider):
        worker_known = True

        def active_now() -> set[str]:
            return set(active_provider())
    elif active_chats is not None:
        worker_known = True
        fixed = set(active_chats)

        def active_now() -> set[str]:
            return fixed
    else:
        active = _active_chats()
        # If the worker state can't be read, we can't prove a chat is idle — so
        # we do NOT evict chats (only sweep stray files). Never risk an active chat.
        worker_known = active is not None
        if not worker_known:
            logger.warning("Worker state unknown — skipping chat eviction (strays only)")
        _http_active = active or set()

        def active_now() -> set[str]:
            return _http_active

    active = active_now()
    idle = [c for c in _idle_chats(ttl_hours) if c not in active] if worker_known else []
    logger.info(
        "Cleanup start | ttl=%.1fh | dry_run=%s | worker_known=%s | idle chats=%d | active=%s",
        ttl_hours, dry_run, worker_known, len(idle), sorted(active),
    )

    freed = 0
    orphans = 0
    skipped = 0
    for chat_id in idle:
        # Re-validate at eviction time: skip if the chat went active or was used
        # again since the idle query. Narrows the race to microseconds.
        if chat_id in active_now() or not _still_idle(chat_id, int(ttl_hours * 3600)):
            logger.info("Skip chat %s — became active since the idle query", chat_id)
            skipped += 1
            continue
        chat_freed, chat_orphans = _evict_chat(chat_id, _backed_filenames(chat_id), dry_run)
        freed += chat_freed
        orphans += chat_orphans
        logger.info(
            "Evicted chat %s | freed %.1f KB | orphans kept=%d",
            chat_id, chat_freed / 1024, chat_orphans,
        )

    empty = _sweep_empty_chat_dirs(active_now(), dry_run) if worker_known else 0
    strays = _sweep_stray_files(ttl_hours * 3600, dry_run)
    summary = {
        "dry_run": dry_run,
        "worker_known": worker_known,
        "idle_chats": len(idle),
        "skipped_active": skipped,
        "freed_bytes": freed,
        "orphans_kept": orphans,
        "empty_dirs_removed": empty,
        "strays_removed": strays,
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    logger.info("Cleanup done | %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Evict idle chat cache from working_dir/")
    ap.add_argument(
        "--ttl-hours", type=float, default=CACHE_TTL_HOURS,
        help="Idle hours before a chat's cache may be evicted (default: %(default)s)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be deleted without deleting anything",
    )
    args = ap.parse_args()
    run_once(args.ttl_hours, dry_run=args.dry_run)
