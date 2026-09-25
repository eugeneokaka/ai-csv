"""Chat API — CRUD, enqueue ask to the worker service, DB-backed file listings.

Execution lives in the worker service (`worker.py`). This module forwards each
ask over HTTP and returns the same `AskResponse` shape as before, so the
frontend is unchanged.
"""

import base64
import json
import logging
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import documents
from auth import current_user_id, require_chat_owner
from config import DATABASE_URL, WORKER_URL  # noqa: F401
from db import get_conn, init_db

router = APIRouter()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chat")

WORKDIR = Path(__file__).parent / "working_dir"
UPLOADS_ROOT = WORKDIR / "uploads"
OUTPUT_ROOT = WORKDIR / "output"


def _uploads_dir(chat_id: str) -> Path:
    d = UPLOADS_ROOT / chat_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _output_dir(chat_id: str) -> Path:
    d = OUTPUT_ROOT / chat_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- Pydantic models ---

class AskRequest(BaseModel):
    chat_id: str
    question: str
    selected_file: str | None = None


class AskResponse(BaseModel):
    stdout: str
    stderr: str
    files: list[dict]
    duration_s: float
    attempts: int
    opencode_session_id: str | None = None
    # Test-only hint: "queued" while the worker pool is saturated, else None.
    status: str | None = None


class CreateChatRequest(BaseModel):
    title: str | None = None


class UpdateChatRequest(BaseModel):
    title: str


# --- DB helpers ---

def _save_prompt(chat_id: str, question: str, selected_file: str | None = None) -> str:
    prompt_id = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO prompt (id, chat_id, role, question) VALUES (%s, %s, 'user', %s)",
        (prompt_id, chat_id, question),
    )
    conn.commit()
    cur.close()
    conn.close()
    return prompt_id


def _get_prompts(chat_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT question, answer, files, duration_s, created_at FROM prompt WHERE chat_id = %s ORDER BY created_at",
        (chat_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# --- Worker helpers ---

async def _worker_busy() -> bool:
    """True when the worker pool is saturated (so this ask will wait in line)."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{WORKER_URL}/load")
            r.raise_for_status()
            data = r.json()
            return data.get("running", 0) >= data.get("max_workers", 2)
    except Exception:
        return False


async def _call_worker(chat_id: str, prompt_id: str, question: str,
                       selected_file: str | None) -> dict:
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(
            f"{WORKER_URL}/run",
            json={
                "chat_id": chat_id,
                "prompt_id": prompt_id,
                "question": question,
                "selected_file": selected_file,
            },
        )
        r.raise_for_status()
        return r.json()


# --- API Endpoints ---

@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, user_id: str = Depends(current_user_id)):
    require_chat_owner(req.chat_id, user_id)
    logger.info("ASK | chat=%s | q=%r", req.chat_id, req.question[:60])

    prompt_id = _save_prompt(req.chat_id, req.question, req.selected_file)

    queued = await _worker_busy()
    if queued:
        logger.info("Worker pool busy — request %s will wait in queue", prompt_id)

    try:
        result = await _call_worker(req.chat_id, prompt_id, req.question, req.selected_file)
    except Exception as e:
        logger.error("Worker call failed: %s", e)
        raise HTTPException(502, f"Worker unavailable: {e}")

    logger.info(
        "DONE | queued=%s | attempts=%d | stdout=%d bytes | stderr=%d bytes | files=%d",
        queued, result["attempts"], len(result["stdout"]),
        len(result["stderr"]), len(result["files"]),
    )
    logger.info("Work details are in the worker process logs (worker.py window).")

    return AskResponse(
        stdout=result["stdout"],
        stderr=result["stderr"],
        files=result["files"],
        duration_s=result["duration_s"],
        attempts=result["attempts"],
        opencode_session_id=result.get("opencode_session_id"),
        status="queued" if queued else None,
    )


# --- Chat CRUD endpoints ---

@router.get("/worker-load")
async def worker_load(user_id: str = Depends(current_user_id)):
    """Proxy the worker pool stats so the UI can show Queued vs Processing."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{WORKER_URL}/load")
            r.raise_for_status()
            data = r.json()
        running = data.get("running", 0)
        max_workers = data.get("max_workers", 2)
        return {
            "max_workers": max_workers,
            "running": running,
            "queued": data.get("queued", 0),
            "busy": running >= max_workers,
        }
    except Exception as e:
        logger.warning("worker-load failed: %s", e)
        return {"max_workers": 0, "running": 0, "queued": 0, "busy": False}


@router.get("/sessions")
def list_sessions(user_id: str = Depends(current_user_id)):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, created_at, updated_at FROM chat WHERE user_id = %s ORDER BY updated_at DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    for r in rows:
        for k in ("created_at", "updated_at"):
            if r[k]:
                r[k] = r[k].isoformat()
    return {"sessions": rows}


@router.post("/sessions")
def create_session(req: CreateChatRequest, user_id: str = Depends(current_user_id)):
    chat_id = str(uuid.uuid4())
    title = req.title or "New Chat"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chat (id, user_id, title) VALUES (%s, %s, %s)",
        (chat_id, user_id, title),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"id": chat_id, "title": title}


@router.put("/sessions/{chat_id}")
def update_session(chat_id: str, req: UpdateChatRequest, user_id: str = Depends(current_user_id)):
    require_chat_owner(chat_id, user_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE chat SET title = %s, updated_at = NOW() WHERE id = %s", (req.title, chat_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}


@router.delete("/sessions/{chat_id}")
def delete_session(chat_id: str, user_id: str = Depends(current_user_id)):
    require_chat_owner(chat_id, user_id)
    import shutil

    for doc in documents.list_documents(chat_id):
        documents.delete_document(doc["id"])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM chat WHERE id = %s", (chat_id,))
    conn.commit()
    cur.close()
    conn.close()

    for d in (_uploads_dir(chat_id), _output_dir(chat_id), WORKDIR / chat_id):
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@router.get("/history")
def history(chat_id: str, user_id: str = Depends(current_user_id)):
    require_chat_owner(chat_id, user_id)
    prompts = _get_prompts(chat_id)
    messages = []
    for p in prompts:
        if p["question"]:
            messages.append({"role": "user", "text": p["question"]})
        if p["answer"]:
            messages.append({
                "role": "assistant",
                "text": p["answer"],
                "files": p["files"] or [],
                "duration_s": p["duration_s"],
            })
    return {"messages": messages}


@router.get("/files")
def list_all_files_endpoint(chat_id: str, user_id: str = Depends(current_user_id)):
    """List all files for this chat: ready documents + unsaved local outputs."""
    require_chat_owner(chat_id, user_id)
    files = []
    seen: set[str] = set()
    for doc in documents.list_documents(chat_id):
        if doc["status"] != "ready":
            continue
        source = "uploads" if doc["source"] == documents.UPLOAD else "output"
        files.append({"name": doc["filename"], "source": source})
        seen.add(doc["filename"])
    import os
    out = _output_dir(chat_id)
    for f in sorted(out.iterdir(), key=os.path.getmtime, reverse=True):
        if f.is_file() and f.suffix in (".csv", ".xlsx", ".png", ".json") and f.name not in seen:
            files.append({"name": f.name, "source": "output"})
    return {"files": files}


@router.get("/download/{filename}")
def download_file(filename: str, chat_id: str, user_id: str = Depends(current_user_id)):
    """Download a file. Local cache first, then S3 (owner only)."""
    from fastapi.responses import FileResponse
    require_chat_owner(chat_id, user_id)
    name = Path(filename).name
    out = _output_dir(chat_id) / name
    path = out if out.exists() else _uploads_dir(chat_id) / name
    if not path.exists():
        doc = documents.get_document(chat_id, name)
        if doc and doc["status"] == "ready":
            try:
                path = documents.ensure_local(doc)
            except Exception as e:
                raise HTTPException(502, f"Could not fetch from S3: {e}")
        else:
            raise HTTPException(404, "File not found")
    return FileResponse(path, filename=name)


@router.get("/outputs")
def list_outputs(chat_id: str, user_id: str = Depends(current_user_id)):
    """List all output files for this chat (owner only)."""
    require_chat_owner(chat_id, user_id)
    files = []
    seen: set[str] = set()
    output_dir = _output_dir(chat_id)
    for f in sorted(output_dir.iterdir()):
        if f.is_file():
            data = f.read_bytes()
            files.append({
                "name": f.name,
                "media_type": "image/png" if f.suffix == ".png" else "application/octet-stream",
                "content_base64": base64.b64encode(data).decode(),
            })
            seen.add(f.name)
    for doc in documents.list_documents(chat_id, documents.OUTPUT):
        if doc["status"] != "ready" or doc["filename"] in seen:
            continue
        try:
            data = documents.ensure_local(doc).read_bytes()
        except Exception:
            continue
        files.append({
            "name": doc["filename"],
            "media_type": "image/png" if doc["filename"].endswith(".png") else "application/octet-stream",
            "content_base64": base64.b64encode(data).decode(),
        })
    return {"files": files}
