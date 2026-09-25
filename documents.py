"""Document metadata — the index of record for files stored in S3.

Every uploaded file (and every AI output the user explicitly saves) gets a row
here. The local disk is only a cache; `ensure_local()` rehydrates on demand.
"""

import logging
from pathlib import Path

import hashlib
import uuid

import s3
from db import get_conn

logger = logging.getLogger("documents")

WORKDIR = Path(__file__).parent / "working_dir"
UPLOADS_ROOT = WORKDIR / "uploads"
OUTPUT_ROOT = WORKDIR / "output"

UPLOAD = "upload"
OUTPUT = "output"


def _query(sql: str, params: tuple = ()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def _execute(sql: str, params: tuple = ()) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    cur.close()
    conn.close()


def create_pending(
    document_id: str,
    chat_id: str,
    user_id: str,
    filename: str,
    source: str,
    content_type: str | None = None,
    size_bytes: int | None = None,
) -> str:
    """Insert a `pending` row *before* the S3 upload (row-first, key later)."""
    _execute(
        """INSERT INTO document
               (id, chat_id, user_id, filename, source, content_type, size_bytes, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')""",
        (document_id, chat_id, user_id, filename, source, content_type, size_bytes),
    )
    return document_id


def mark_ready(document_id: str, s3_key: str, checksum: str | None = None) -> None:
    _execute(
        """UPDATE document
              SET s3_key = %s, checksum = %s, status = 'ready', last_accessed_at = NOW()
            WHERE id = %s""",
        (s3_key, checksum, document_id),
    )


def mark_failed(document_id: str) -> None:
    _execute("UPDATE document SET status = 'failed' WHERE id = %s", (document_id,))


def get_by_id(document_id: str) -> dict | None:
    rows = _query("SELECT * FROM document WHERE id = %s", (document_id,))
    return rows[0] if rows else None


def get_document(chat_id: str, filename: str, source: str | None = None) -> dict | None:
    """Newest document matching a chat + filename (optionally a specific source)."""
    if source:
        rows = _query(
            """SELECT * FROM document
                WHERE chat_id = %s AND filename = %s AND source = %s
                ORDER BY created_at DESC LIMIT 1""",
            (chat_id, filename, source),
        )
    else:
        rows = _query(
            """SELECT * FROM document
                WHERE chat_id = %s AND filename = %s
                ORDER BY created_at DESC LIMIT 1""",
            (chat_id, filename),
        )
    return rows[0] if rows else None


def list_documents(chat_id: str, source: str | None = None) -> list[dict]:
    if source:
        return _query(
            "SELECT * FROM document WHERE chat_id = %s AND source = %s ORDER BY created_at",
            (chat_id, source),
        )
    return _query(
        "SELECT * FROM document WHERE chat_id = %s ORDER BY created_at",
        (chat_id,),
    )


def delete_document(document_id: str) -> None:
    """Remove the row and its S3 object (best effort on S3)."""
    doc = get_by_id(document_id)
    if not doc:
        return
    if doc.get("s3_key"):
        try:
            s3.delete_object(doc["s3_key"])
        except Exception as exc:  # pragma: no cover - network best effort
            logger.warning("S3 delete failed for %s: %s", doc["s3_key"], exc)
    _execute("DELETE FROM document WHERE id = %s", (document_id,))


def replace_existing(chat_id: str, filename: str, source: str = UPLOAD) -> int:
    """Drop any prior documents with the same chat/filename/source (overwrite)."""
    rows = _query(
        "SELECT id FROM document WHERE chat_id = %s AND filename = %s AND source = %s",
        (chat_id, filename, source),
    )
    for row in rows:
        delete_document(row["id"])
    return len(rows)


# --- Local cache helpers ---

def local_path(doc: dict) -> Path:
    root = UPLOADS_ROOT if doc["source"] == UPLOAD else OUTPUT_ROOT
    return root / doc["chat_id"] / doc["filename"]


def ensure_local(doc: dict) -> Path:
    """Return the local path, downloading from S3 if the cache is cold."""
    path = local_path(doc)
    if path.exists():
        return path
    if not doc.get("s3_key"):
        raise FileNotFoundError(
            f"{doc['filename']} has no S3 object yet (status={doc.get('status')})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(s3.download_bytes(doc["s3_key"]))
    _execute("UPDATE document SET last_accessed_at = NOW() WHERE id = %s", (doc["id"],))
    return path


def save_local_output(
    chat_id: str,
    user_id: str,
    filename: str,
    local_path: Path,
    content_type: str | None = None,
) -> str:
    """Persist an existing local output file to S3 as a `source='output'` document."""
    replace_existing(chat_id, filename, OUTPUT)
    document_id = str(uuid.uuid4())
    key = s3.document_key(chat_id, document_id, filename)
    data = local_path.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()

    create_pending(
        document_id, chat_id, user_id, filename, OUTPUT,
        content_type=content_type, size_bytes=len(data),
    )
    try:
        s3.upload_bytes(key, data, content_type=content_type)
    except Exception:
        mark_failed(document_id)
        raise
    mark_ready(document_id, key, checksum=checksum)
    return document_id

