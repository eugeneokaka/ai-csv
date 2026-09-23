"""Chat: user question -> helper preview -> OpenCode -> execute code."""

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import current_user_id, require_chat_owner
from config import MODEL_ID, MODEL_PROVIDER, OPENCODE_URL
from db import get_conn, init_db

router = APIRouter()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chat")

MAX_FIX_RETRIES = 3
CODE_TIMEOUT = 60

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


class CreateChatRequest(BaseModel):
    title: str | None = None


class UpdateChatRequest(BaseModel):
    title: str


# --- DB helpers ---

def _db_init():
    init_db()


def _chat_exists(chat_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM chat WHERE id = %s", (chat_id,))
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists


def _get_opencode_session(chat_id: str) -> str | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT opencode_session_id FROM chat WHERE id = %s", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["opencode_session_id"] if row else None


def _set_opencode_session(chat_id: str, oc_session_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE chat SET opencode_session_id = %s, updated_at = NOW() WHERE id = %s",
        (oc_session_id, chat_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def _save_prompt(chat_id: str, question: str) -> str:
    """Create a new prompt row when user sends a question. Returns the prompt ID."""
    import uuid
    prompt_id = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO prompt (id, chat_id, role, question)
           VALUES (%s, %s, 'user', %s)""",
        (prompt_id, chat_id, question),
    )
    conn.commit()
    cur.close()
    conn.close()
    return prompt_id


def _update_prompt(prompt_id: str, answer: str = None, code: str = None,
                   files: list[dict] = None, duration_s: float = None,
                   attempts: int = None):
    """Update an existing prompt row with the agent's response."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE prompt
           SET answer = %s, code = %s, files = %s, duration_s = %s, attempts = %s
           WHERE id = %s""",
        (answer, code, json.dumps(files) if files else None, duration_s, attempts, prompt_id),
    )
    conn.commit()
    cur.close()
    conn.close()


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


# --- OpenCode helpers ---

async def opencode_chat(session_id: str, prompt: str) -> str:
    """Send prompt to OpenCode and return the text response."""
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
    """Create a new OpenCode session."""
    logger.info("Creating OpenCode session...")
    try:
        async with httpx.AsyncClient(base_url=OPENCODE_URL, timeout=10) as client:
            r = await client.post("/session", json={"title": "csv-analysis"})
            r.raise_for_status()
            session_id = r.json()["id"]
            logger.info("OpenCode session created: %s", session_id)
            return session_id
    except Exception as e:
        logger.error("Cannot reach OpenCode: %s", e)
        return None


# --- Prompt building ---

def get_preview(chat_id: str, filename: str) -> str:
    """Get first 5 rows + columns from a CSV. Checks output first, then uploads."""
    import pandas as pd
    out = _output_dir(chat_id) / filename
    path = out if out.exists() else _uploads_dir(chat_id) / filename
    df = pd.read_csv(path)
    cols = list(df.columns)
    preview = df.head(5).to_string(index=False)
    return f"Columns: {cols}\n5 rows:\n{preview}"


def list_session_files(chat_id: str) -> list[str]:
    """List CSV files from output (latest first) and uploads for this chat."""
    files = []
    for f in sorted(_output_dir(chat_id).glob("*.csv"), key=os.path.getmtime, reverse=True):
        files.append(f.name)
    for f in sorted(_uploads_dir(chat_id).glob("*.csv")):
        if f.name not in files:
            files.append(f.name)
    return files


def list_all_files(chat_id: str) -> list[dict]:
    """List all files from both this chat's uploads/ and output/."""
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
        previews = []
        for f in files:
            previews.append(f"--- {f} ---\n{get_preview(chat_id, f)}")
        data_desc = "\n\n".join(previews)

    file_list = "\n".join([f"  - {f['name']} ({f['source']})" for f in all_files]) if all_files else "  (none)"

    selected_hint = ""
    if selected_file:
        selected_hint = f"\n\nUser selected file: {selected_file} — work with this file. Use helper.get_full_csv(\"{selected_file}\") to load it."

    print("=== PROMPT ===")
    print(f"Chat: {chat_id}")
    print(f"Question: {question}")
    print(f"Available files: {files}")
    print(f"Selected file: {selected_file}")
    print(f"Data preview:\n{data_desc}")
    return f"{SYSTEM_PROMPT}\n\nAvailable files:\n{file_list}\n\nData preview:\n{data_desc}{selected_hint}\n\nQuestion: {question}"


# --- Code extraction and execution ---

def extract_code(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    for fence in ("```python", "```py", "```"):
        if fence in text:
            after = text.split(fence, 1)[1]
            return after.split("```", 1)[0].strip()
    lines = text.splitlines()
    code_lines = []
    for line in lines:
        s = line.strip()
        if code_lines or s.startswith(("import ", "from ", "def ", "#")):
            code_lines.append(line)
    return "\n".join(code_lines).strip()


def run_code(chat_id: str, code: str) -> dict:
    logger.info("--- EXECUTING CODE (chat=%s) ---", chat_id)
    logger.debug("Code:\n%s", code)

    run_dir = _run_dir(chat_id)
    output_dir = _output_dir(chat_id)
    app_file = run_dir / "app.py"
    app_file.write_text(code, encoding="utf-8")

    # Snapshot existing files so we only return NEW ones after execution
    existing_files = set(f.name for f in output_dir.iterdir() if f.is_file())

    start = time.monotonic()
    try:
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "CHAT_ID": chat_id,
        }
        # Add api/ directory to PYTHONPATH so `import helper` works from the run dir
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

    # Auto-heal: move stray files written to a relative output/ dir into the chat's output dir
    stray_dir = run_dir / "output"
    if stray_dir.exists() and stray_dir.is_dir():
        for f in stray_dir.iterdir():
            if f.is_file():
                dest = output_dir / f.name
                shutil.move(str(f), str(dest))
                logger.warning("Moved stray output file: %s -> %s", f.name, dest)

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

    return {"stdout": stdout, "stderr": stderr, "files": files, "duration_s": duration, "returncode": rc}


# --- API Endpoints ---

@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, user_id: str = Depends(current_user_id)):
    require_chat_owner(req.chat_id, user_id)
    logger.info("========================================")
    logger.info("NEW REQUEST")
    logger.info("Chat ID: %s", req.chat_id)
    logger.info("Question: %s", req.question)
    logger.info("========================================")

    # Create user prompt row in DB
    prompt_id = _save_prompt(req.chat_id, req.question)

    # Get or create OpenCode session for this chat
    oc_session = _get_opencode_session(req.chat_id)
    if not oc_session:
        oc_session = await create_opencode_session()
        if not oc_session:
            _update_prompt(prompt_id, answer="OpenCode server not running.", attempts=1)
            return AskResponse(stdout="OpenCode server not running. Start it first.", stderr="", files=[], duration_s=0.0, attempts=1, opencode_session_id=None)
        _set_opencode_session(req.chat_id, oc_session)

    prompt = build_prompt(req.chat_id, req.question, req.selected_file)
    try:
        reply = await opencode_chat(oc_session, prompt)
    except Exception as e:
        logger.error("OpenCode call failed: %s", e)
        _update_prompt(prompt_id, answer=str(e), attempts=1)
        return AskResponse(stdout="", stderr=str(e), files=[], duration_s=0.0, attempts=1, opencode_session_id=oc_session)

    if "NO_CODE:" in reply:
        answer = reply.split("NO_CODE:", 1)[1].strip()
        logger.info("Conversational reply (no code): %s", answer)
        _update_prompt(prompt_id, answer=answer, attempts=1)
        return AskResponse(stdout=answer, stderr="", files=[], duration_s=0.0, attempts=1, opencode_session_id=oc_session)

    code = extract_code(reply)
    if not code:
        # If LLM mentioned saving a file but gave no code, re-prompt for actual code
        keywords = ("saved", "chart", "png", "xlsx", "csv", "output", "graph", "plot")
        if any(kw in reply.lower() for kw in keywords):
            logger.warning("No code extracted but reply mentions output — re-prompting for code")
            fix_prompt = "You mentioned saving output but did not provide Python code. Return ONLY the Python code that generates and saves the output. No explanations."
            try:
                fix_reply = await opencode_chat(oc_session, fix_prompt)
                code = extract_code(fix_reply)
            except Exception:
                pass
        if not code:
            logger.warning("No code extracted from reply")
            _update_prompt(prompt_id, answer=reply.strip(), attempts=1)
            return AskResponse(stdout=reply.strip(), stderr="", files=[], duration_s=0.0, attempts=1, opencode_session_id=oc_session)

    logger.info("Extracted %d chars of code", len(code))
    result = run_code(req.chat_id, code)
    attempts = 1

    while result["stderr"].strip() and attempts < MAX_FIX_RETRIES:
        attempts += 1
        logger.info("--- AUTO-FIX attempt %d ---", attempts)
        fix_prompt = f"Your code errored:\n{result['stderr'][-3000:]}\nFix it. Return ONLY the corrected code."
        try:
            fix_reply = await opencode_chat(oc_session, fix_prompt)
        except Exception as e:
            logger.error("Fix call failed: %s", e)
            break
        code = extract_code(fix_reply)
        if not code:
            logger.warning("No code in fix reply")
            break
        result = run_code(req.chat_id, code)

    logger.info("DONE | attempts=%d | stdout=%d bytes | stderr=%d bytes | files=%d",
                attempts, len(result["stdout"]), len(result["stderr"]), len(result["files"]))

    # Update prompt row with agent response
    _update_prompt(
        prompt_id,
        answer=result["stdout"] or result["stderr"],
        code=code,
        files=result["files"],
        duration_s=result["duration_s"],
        attempts=attempts,
    )

    return AskResponse(
        stdout=result["stdout"],
        stderr=result["stderr"],
        files=result["files"],
        duration_s=result["duration_s"],
        attempts=attempts,
        opencode_session_id=oc_session,
    )


# --- Chat CRUD endpoints ---

@router.get("/sessions")
def list_sessions(user_id: str = Depends(current_user_id)):
    """List all chats for the authenticated user."""
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
    """Create a new chat owned by the authenticated user."""
    import uuid
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
    """Update chat title (owner only)."""
    require_chat_owner(chat_id, user_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE chat SET title = %s, updated_at = NOW() WHERE id = %s",
        (req.title, chat_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}


@router.delete("/sessions/{chat_id}")
def delete_session(chat_id: str, user_id: str = Depends(current_user_id)):
    """Delete a chat and all its prompts (owner only)."""
    require_chat_owner(chat_id, user_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM chat WHERE id = %s", (chat_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}


@router.get("/history")
def history(chat_id: str, user_id: str = Depends(current_user_id)):
    """Get chat history from DB — 1 row per Q&A (owner only)."""
    require_chat_owner(chat_id, user_id)
    prompts = _get_prompts(chat_id)
    messages = []
    for p in prompts:
        if p["question"]:
            messages.append({"role": "user", "text": p["question"]})
        if p["answer"]:
            files = p["files"] or []
            messages.append({
                "role": "assistant",
                "text": p["answer"],
                "files": files,
                "duration_s": p["duration_s"],
            })
    logger.info("Returning %d messages for chat %s", len(messages), chat_id)
    return {"messages": messages}


@router.get("/files")
def list_all_files_endpoint(chat_id: str, user_id: str = Depends(current_user_id)):
    """List all files from this chat's uploads/ and output/ (owner only)."""
    require_chat_owner(chat_id, user_id)
    return {"files": list_all_files(chat_id)}


@router.get("/download/{filename}")
def download_file(filename: str, chat_id: str, user_id: str = Depends(current_user_id)):
    """Download a file from this chat's output/ or uploads/ (owner only)."""
    from fastapi.responses import FileResponse
    require_chat_owner(chat_id, user_id)
    name = Path(filename).name
    out = _output_dir(chat_id) / name
    path = out if out.exists() else _uploads_dir(chat_id) / name
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=name)


@router.get("/outputs")
def list_outputs(chat_id: str, user_id: str = Depends(current_user_id)):
    """List all files in this chat's output folder (owner only)."""
    require_chat_owner(chat_id, user_id)
    output_dir = _output_dir(chat_id)
    files = []
    for f in sorted(output_dir.iterdir()):
        if f.is_file():
            data = f.read_bytes()
            files.append({
                "name": f.name,
                "media_type": "image/png" if f.suffix == ".png" else "application/octet-stream",
                "content_base64": base64.b64encode(data).decode(),
            })
    return {"files": files}
