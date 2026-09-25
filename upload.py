"""Upload: validate -> document row (pending) -> S3 -> document row (ready).

S3 is the durable store. A local copy is still written as a temporary cache
bridge so the chat pipeline keeps working until lazy hydration lands.
"""

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

import documents
import s3
from auth import current_user_id, require_chat_owner

router = APIRouter()

logger = logging.getLogger("upload")

WORKDIR = Path(__file__).parent / "working_dir"
UPLOADS_ROOT = WORKDIR / "uploads"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def chat_upload_dir(chat_id: str) -> Path:
    """Temporary local cache: working_dir/uploads/{chat_id}/"""
    d = UPLOADS_ROOT / chat_id
    d.mkdir(parents=True, exist_ok=True)
    return d


class FileProfile(BaseModel):
    name: str
    rows: int
    columns: list[str]
    column_types: dict[str, str]
    missing_values: dict[str, int]
    numeric_stats: dict[str, dict[str, float]]
    preview: list[dict]


def _store_file(
    chat_id: str,
    user_id: str,
    name: str,
    content: bytes,
    content_type: str,
) -> None:
    """Blocking work (DB + S3 + cache) — run in a thread so the loop stays free."""
    documents.replace_existing(chat_id, name, documents.UPLOAD)

    document_id = str(uuid.uuid4())
    key = s3.document_key(chat_id, document_id, name)
    checksum = hashlib.sha256(content).hexdigest()

    # Row first, key after the upload succeeds.
    documents.create_pending(
        document_id,
        chat_id,
        user_id,
        name,
        documents.UPLOAD,
        content_type=content_type,
        size_bytes=len(content),
    )
    try:
        s3.upload_bytes(key, content, content_type=content_type)
    except Exception as exc:
        documents.mark_failed(document_id)
        logger.error("S3 upload failed for %s: %s", name, exc)
        raise HTTPException(502, f"S3 upload failed: {exc}")

    documents.mark_ready(document_id, key, checksum=checksum)


@router.post("/{chat_id}")
async def upload(
    chat_id: str,
    files: list[UploadFile] = File(...),
    user_id: str = Depends(current_user_id),
):
    require_chat_owner(chat_id, user_id)
    saved = []
    for f in files:
        name = Path(f.filename or "").name  # strip any path components
        if not name or not name.lower().endswith(".csv"):
            raise HTTPException(400, "Only .csv files allowed")
        content = await f.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large (max 50MB)")

        await asyncio.to_thread(
            _store_file,
            chat_id,
            user_id,
            name,
            content,
            f.content_type or "text/csv",
        )
        saved.append(name)
    return {"files": saved}


@router.get("/{chat_id}")
def list_files(chat_id: str, user_id: str = Depends(current_user_id)):
    require_chat_owner(chat_id, user_id)
    docs = documents.list_documents(chat_id, documents.UPLOAD)
    files = [d["filename"] for d in docs if d["status"] == "ready"]
    return {"files": files}


@router.get("/{chat_id}/profile/{filename}", response_model=FileProfile)
def profile_file(
    chat_id: str,
    filename: str,
    user_id: str = Depends(current_user_id),
):
    require_chat_owner(chat_id, user_id)
    name = Path(filename).name
    doc = documents.get_document(chat_id, name, documents.UPLOAD)
    if not doc or doc["status"] != "ready":
        raise HTTPException(404, "File not found")
    try:
        path = documents.ensure_local(doc)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    df = pd.read_csv(path)

    numeric_stats = {}
    missing = {}
    numeric_df = df.select_dtypes(include="number")
    for col in numeric_df.columns:
        s = numeric_df[col]
        numeric_stats[col] = {
            "mean": float(s.mean()),
            "min": float(s.min()),
            "max": float(s.max()),
            "sum": float(s.sum()),
            "std": float(s.std()),
        }
    for col in df.columns:
        missing[col] = int(df[col].isna().sum())

    import json
    preview = json.loads(df.head(50).to_json(orient="records", date_format="iso"))

    return FileProfile(
        name=name,
        rows=len(df),
        columns=list(df.columns),
        column_types={col: str(dtype) for col, dtype in df.dtypes.items()},
        missing_values=missing,
        numeric_stats=numeric_stats,
        preview=preview,
    )


@router.delete("/{chat_id}/{filename}")
def delete_file(
    chat_id: str,
    filename: str,
    user_id: str = Depends(current_user_id),
):
    require_chat_owner(chat_id, user_id)
    name = Path(filename).name
    doc = documents.get_document(chat_id, name, documents.UPLOAD)
    if doc:
        documents.delete_document(doc["id"])
    local = chat_upload_dir(chat_id) / name
    if local.exists():
        local.unlink()
    return {"deleted": name}
