"""Worker service — owns the execution pool that runs generated code.

`chat.py` (the API) forwards each ask here. This process keeps a small
ThreadPoolExecutor (MAX_WORKERS) so only N prompts run at once; the rest wait
in the executor queue. Run as its own process:

    python worker.py

The pipeline is identical to before — it just lives here now.
"""

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

import documents
from config import (
    MAX_WORKERS,
    MODEL_ID,
    MODEL_PROVIDER,
    OPENCODE_URL,
    WORKER_HOST,
    WORKER_PORT,
)
from db import get_conn, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("worker")
logger.setLevel(logging.DEBUG)  # verbose pipeline logs; noisy libs stay at INFO

MAX_FIX_RETRIES = 3
CODE_TIMEOUT = 60

WORKDIR = Path(__file__).parent / "working_dir"
UPLOADS_ROOT = WORKDIR / "uploads"
OUTPUT_ROOT = WORKDIR / "output"

# Only MAX_WORKERS prompts run at once; extras queue inside the executor.
pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="worker")
_running = 0
_lock = threading.Lock()


def _uploads_dir(chat_id: str) -> Path:
    d = UPLOADS_ROOT / chat_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _output_dir(chat_id: str) -> Path:
    d = OUTPUT_ROOT / chat_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_dir(chat_id: str) -> Path:
    d = WORKDIR / chat_id
    d.mkdir(parents=True, exist_ok=True)
    return d


SYSTEM_PROMPT = """You are an AI data analyst. Generate Python code to analyze CSV data.

STRICT RULES:
- Use `import helper` for ALL file I/O — no exceptions. Never build file paths yourself.
- Your working directory is private to this chat. helper already points at the right folders.
- To load data use: df = helper.get_first_csv() — this loads the latest CSV for this chat
- To save edited CSVs: helper.save_csv(df, "name.csv")
- To save charts as images: helper.save_chart(fig, "name.png")
- To save Excel with data: helper.save_excel(df, "name.xlsx")
- To save Excel with an embedded chart: helper.save_chart_to_excel(df, "x_col", "y_col", "chart.xlsx") — use this when user asks for chart IN a sheet
- To get full output path: helper.get_output_path("file.xlsx") or helper.get_output_dir()
- To openpyxl style Excel: helper.save_excel(df, "out.xlsx") then wb = helper.load_workbook("out.xlsx") then style wb then wb.save(helper.get_output_path("out.xlsx"))
- NEVER create temp files — save once, load with helper.load_workbook(), style, save again
- NEVER use os, sys, subprocess, open() on files — use helper functions only
- NEVER use df.to_csv(), df.to_excel(), plt.savefig(), open() for output files — use helper functions only
- NEVER hardcode absolute paths — use helper.get_output_path() or helper.get_output_dir()
- NEVER use matplotlib to embed charts in Excel — use helper.save_chart_to_excel() instead
- ALWAYS print results to stdout in human-readable format
- ALWAYS save any edited data or charts to output via helper — do NOT just print, also save
- Keep answers short and friendly — this goes to a non-technical user
- Format numbers nicely (round, commas, %)
- If the question is NOT about data (greeting, meta), output exactly: NO_CODE: <your answer>

You answer with ONLY the Python code — no markdown, no explanations.
"""


# --- Hydration ---

def hydrate_documents(chat_id: str) -> None:
    """Download this chat's ready documents from S3 into the local cache."""
    for doc in documents.list_documents(chat_id):
        if doc["status"] != "ready" or not doc.get("s3_key"):
            continue
        try:
            documents.ensure_local(doc)
        except Exception as e:
            logger.warning("Hydration failed for %s: %s", doc["filename"], e)


# --- OpenCode helpers ---

async def opencode_chat(session_id: str, prompt: str) -> str:
    logger.info("--- OPENCODE REQUEST ---")
    logger.info("Session: %s | Prompt length: %d chars", session_id, len(prompt))
    logger.debug("Prompt:\n%s", prompt[:2000])
    async with httpx.AsyncClient(base_url=OPENCODE_URL, timeout=180) as client:
        msg = await client.post(
            f"/session/{session_id}/message",
            json={
                "model": {"providerID": MODEL_PROVIDER, "modelID": MODEL_ID},
                "parts": [{"type": "text", "text": prompt}],
            },
        )
        msg.raise_for_status()
        data = msg.json()
        parts = data.get("parts", [])
        reply = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
        logger.info("--- OPENCODE RESPONSE ---")
        logger.info("Reply length: %d chars", len(reply))
        logger.debug("Reply:\n%s", reply[:2000])
        return reply


async def create_opencode_session() -> str | None:
    logger.info("Creating OpenCode session...")
    try:
        async with httpx.AsyncClient(base_url=OPENCODE_URL, timeout=10) as client:
            r = await client.post("/session", json={"title": "csv-analysis"})
            r.raise_for_status()
            sid = r.json()["id"]
            logger.info("OpenCode session created: %s", sid)
            return sid
    except Exception as e:
        logger.error("Cannot reach OpenCode: %s", e)
        return None


# --- Prompt building ---

def get_preview(chat_id: str, filename: str) -> str:
    import pandas as pd
    out = _output_dir(chat_id) / filename
    path = out if out.exists() else _uploads_dir(chat_id) / filename
    df = pd.read_csv(path)
    return f"Columns: {list(df.columns)}\n5 rows:\n{df.head(5).to_string(index=False)}"


def list_session_files(chat_id: str) -> list[str]:
    files = []
    for f in sorted(_output_dir(chat_id).glob("*.csv"), key=os.path.getmtime, reverse=True):
        files.append(f.name)
    for f in sorted(_uploads_dir(chat_id).glob("*.csv")):
        if f.name not in files:
            files.append(f.name)
    return files


def list_all_files(chat_id: str) -> list[dict]:
    files = []
    for f in sorted(_uploads_dir(chat_id).iterdir()):
        if f.is_file() and f.suffix in (".csv", ".xlsx", ".png", ".json"):
            files.append({"name": f.name, "source": "uploads"})
    for f in sorted(_output_dir(chat_id).iterdir(), key=os.path.getmtime, reverse=True):
        if f.is_file() and f.suffix in (".csv", ".xlsx", ".png", ".json"):
            files.append({"name": f.name, "source": "output"})
    return files


def build_prompt(chat_id: str, question: str, selected_file: str = None) -> str:
    files = list_session_files(chat_id)
    all_files = list_all_files(chat_id)
    if not files:
        data_desc = "No files uploaded yet."
    else:
        data_desc = "\n\n".join(f"--- {f} ---\n{get_preview(chat_id, f)}" for f in files)
    file_list = (
        "\n".join(f"  - {f['name']} ({f['source']})" for f in all_files)
        if all_files else "  (none)"
    )
    selected_hint = ""
    if selected_file:
        selected_hint = (
            f"\n\nUser selected file: {selected_file} — work with this file. "
            f'Use helper.get_full_csv("{selected_file}") to load it.'
        )

    logger.info("=== PROMPT ===")
    logger.info("Chat: %s", chat_id)
    logger.info("Question: %s", question)
    logger.info("Available files: %s", files)
    logger.info("Selected file: %s", selected_file)
    logger.info("Data preview:\n%s", data_desc)

    return (
        f"{SYSTEM_PROMPT}\n\nAvailable files:\n{file_list}\n\n"
        f"Data preview:\n{data_desc}{selected_hint}\n\nQuestion: {question}"
    )


# --- Code extraction and execution ---

def extract_code(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for fence in ("```python", "```py", "```"):
        if fence in text:
            after = text.split(fence, 1)[1]
            return after.split("```", 1)[0].strip()
    code_lines = []
    for line in text.splitlines():
        s = line.strip()
        if code_lines or s.startswith(("import ", "from ", "def ", "#")):
            code_lines.append(line)
    return "\n".join(code_lines).strip()


def run_code(chat_id: str, code: str) -> dict:
    logger.info("--- EXECUTING CODE (chat=%s) ---", chat_id)
    logger.debug("Code:\n%s", code)
    run_dir = _run_dir(chat_id)
    output_dir = _output_dir(chat_id)
    (run_dir / "app.py").write_text(code, encoding="utf-8")

    existing_files = set(f.name for f in output_dir.iterdir() if f.is_file())
    start = time.monotonic()
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CHAT_ID": chat_id}
        api_dir = str(Path(__file__).parent)
        env["PYTHONPATH"] = api_dir + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "app.py"],
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            timeout=CODE_TIMEOUT,
            env=env,
        )
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode("utf-8", "replace") if e.stdout else ""
        stderr = f"TIMEOUT: code ran longer than {CODE_TIMEOUT}s"
        rc = -1
    duration = time.monotonic() - start
    logger.info("Execution done in %.2fs | returncode=%d", duration, rc)
    if stdout.strip():
        logger.info("STDOUT:\n%s", stdout.strip())
    if stderr.strip():
        logger.warning("STDERR:\n%s", stderr.strip())

    stray = run_dir / "output"
    if stray.exists() and stray.is_dir():
        for f in stray.iterdir():
            if f.is_file():
                shutil.move(str(f), str(output_dir / f.name))
                logger.warning("Moved stray output file: %s", f.name)

    files = []
    for f in sorted(output_dir.iterdir()):
        if f.is_file() and f.name not in existing_files:
            data = f.read_bytes()
            files.append({
                "name": f.name,
                "media_type": "image/png" if f.suffix == ".png" else "application/octet-stream",
                "content_base64": base64.b64encode(data).decode(),
            })
    if files:
        logger.info("Output files: %s", [f["name"] for f in files])
    if not stdout.strip() and not stderr.strip() and not files and rc == 0:
        stderr = "Code ran but produced no output. Use print() or helper.save_csv()/helper.save_chart()."
    return {"stdout": stdout, "stderr": stderr, "files": files,
            "duration_s": duration, "returncode": rc}


# --- DB helpers ---

def get_opencode_session(chat_id: str) -> str | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT opencode_session_id FROM chat WHERE id = %s", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["opencode_session_id"] if row else None


def set_opencode_session(chat_id: str, oc_session_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE chat SET opencode_session_id = %s, updated_at = NOW() WHERE id = %s",
        (oc_session_id, chat_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def update_prompt(prompt_id: str, answer: str, code: str | None,
                  files: list[dict], duration_s: float, attempts: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE prompt SET answer=%s, code=%s, files=%s, duration_s=%s, attempts=%s
           WHERE id=%s""",
        (answer, code, json.dumps(files) if files else None, duration_s, attempts, prompt_id),
    )
    conn.commit()
    cur.close()
    conn.close()


# --- Job shape ---

class RunRequest(BaseModel):
    chat_id: str
    prompt_id: str
    question: str
    selected_file: str | None = None


class RunResponse(BaseModel):
    stdout: str
    stderr: str
    files: list[dict]
    duration_s: float
    attempts: int
    opencode_session_id: str | None = None


async def execute(chat_id: str, prompt_id: str, question: str,
                  selected_file: str | None) -> dict:
    """The full ask pipeline — same logic that used to live in chat.py:ask()."""
    hydrate_documents(chat_id)

    oc_session = get_opencode_session(chat_id)
    if not oc_session:
        oc_session = await create_opencode_session()
        if not oc_session:
            update_prompt(prompt_id, "OpenCode server not running.", None, [], 0.0, 1)
            return {"stdout": "OpenCode server not running. Start it first.", "stderr": "",
                    "files": [], "duration_s": 0.0, "attempts": 1, "opencode_session_id": None}
        set_opencode_session(chat_id, oc_session)

    prompt = build_prompt(chat_id, question, selected_file)
    try:
        reply = await opencode_chat(oc_session, prompt)
    except Exception as e:
        logger.error("OpenCode call failed: %s", e)
        update_prompt(prompt_id, str(e), None, [], 0.0, 1)
        return {"stdout": "", "stderr": str(e), "files": [], "duration_s": 0.0,
                "attempts": 1, "opencode_session_id": oc_session}

    if "NO_CODE:" in reply:
        answer = reply.split("NO_CODE:", 1)[1].strip()
        update_prompt(prompt_id, answer, None, [], 0.0, 1)
        return {"stdout": answer, "stderr": "", "files": [], "duration_s": 0.0,
                "attempts": 1, "opencode_session_id": oc_session}

    code = extract_code(reply)
    if not code:
        keywords = ("saved", "chart", "png", "xlsx", "csv", "output", "graph", "plot")
        if any(kw in reply.lower() for kw in keywords):
            fix_prompt = ("You mentioned saving output but did not provide Python code. "
                          "Return ONLY the Python code that generates and saves the output.")
            try:
                code = extract_code(await opencode_chat(oc_session, fix_prompt))
            except Exception:
                pass
        if not code:
            update_prompt(prompt_id, reply.strip(), None, [], 0.0, 1)
            return {"stdout": reply.strip(), "stderr": "", "files": [], "duration_s": 0.0,
                    "attempts": 1, "opencode_session_id": oc_session}

    result = run_code(chat_id, code)
    attempts = 1
    while result["stderr"].strip() and attempts < MAX_FIX_RETRIES:
        attempts += 1
        logger.info("--- AUTO-FIX attempt %d ---", attempts)
        fix_prompt = (f"Your code errored:\n{result['stderr'][-3000:]}\n"
                      "Fix it. Return ONLY the corrected code.")
        try:
            fix_reply = await opencode_chat(oc_session, fix_prompt)
        except Exception as e:
            logger.error("Fix call failed: %s", e)
            break
        code = extract_code(fix_reply)
        if not code:
            break
        result = run_code(chat_id, code)

    update_prompt(prompt_id, result["stdout"] or result["stderr"], code,
                  result["files"], result["duration_s"], attempts)
    return {"stdout": result["stdout"], "stderr": result["stderr"], "files": result["files"],
            "duration_s": result["duration_s"], "attempts": attempts,
            "opencode_session_id": oc_session}


def _run_sync(req: RunRequest) -> dict:
    """Called inside the pool thread — owns the running counter."""
    global _running
    with _lock:
        _running += 1
    try:
        import asyncio
        return asyncio.run(execute(req.chat_id, req.prompt_id, req.question, req.selected_file))
    finally:
        with _lock:
            _running -= 1


# --- HTTP API ---

app = FastAPI(title="AI CSV Analyzer Worker")
router = APIRouter()


@router.post("/run", response_model=RunResponse)
async def run(req: RunRequest):
    logger.info("RUN | chat=%s prompt=%s q=%r", req.chat_id, req.prompt_id, req.question[:60])
    loop = __import__("asyncio").get_event_loop()
    result = await loop.run_in_executor(pool, _run_sync, req)
    return RunResponse(**result)


@router.get("/load")
def load():
    """Pool stats — the API uses this to decide whether to show 'queued'."""
    with _lock:
        running = _running
    queued = max(0, pool._work_queue.qsize())  # noqa: SLF001 (introspection)
    return {"max_workers": MAX_WORKERS, "running": running, "queued": queued}


@router.get("/health")
def health():
    return {"status": "ok"}


app.include_router(router)


if __name__ == "__main__":
    import uvicorn
    init_db()
    logger.info("Worker service starting | max_workers=%d port=%d", MAX_WORKERS, WORKER_PORT)
    uvicorn.run("worker:app", host=WORKER_HOST, port=WORKER_PORT, reload=False)
