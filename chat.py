"""Chat: user question -> helper preview -> OpenCode -> execute code."""

import base64
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from config import MODEL_ID, MODEL_PROVIDER, OPENCODE_URL

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
UPLOADS = WORKDIR / "uploads"
OUTPUT = WORKDIR / "output"
APP_FILE = WORKDIR / "app.py"

SYSTEM_PROMPT = """You are an AI data analyst. Generate Python code to analyze CSV data.

STRICT RULES:
- ONLY touch files in working_dir/ (uploads/ for reading, output/ for writing)
- Your cwd IS working_dir — so output/ means working_dir/output. NEVER write to api/output.
- Use `import helper` for ALL file I/O — no exceptions
- To load data use: df = helper.get_first_csv() — this loads the first uploaded CSV
- To save edited CSVs: helper.save_csv(df, "name.csv")
- To save charts as images: helper.save_chart(fig, "name.png")
- To save Excel with data: helper.save_excel(df, "name.xlsx")
- To save Excel with an embedded chart: helper.save_chart_to_excel(df, "x_col", "y_col", "chart.xlsx") — use this when user asks for chart IN a sheet
- To get full output path: helper.get_output_path("file.xlsx") or helper.get_output_dir()
- To openpyxl style Excel: helper.save_excel(df, "out.xlsx") then wb = helper.load_workbook("out.xlsx") then style wb then wb.save(helper.get_output_path("out.xlsx"))
- NEVER create temp files — save once, load with helper.load_workbook(), style, save again
- NEVER use os, sys, subprocess, open() on files outside working_dir/
- NEVER use df.to_csv(), df.to_excel(), plt.savefig(), open() for output files — use helper functions only
- NEVER hardcode absolute paths — use helper.get_output_path() or helper.get_output_dir()
- NEVER use matplotlib to embed charts in Excel — use helper.save_chart_to_excel() instead
- ALWAYS print results to stdout in human-readable format
- ALWAYS save any edited data or charts to output/ via helper — do NOT just print, also save
- Keep answers short and friendly — this goes to a non-technical user
- Format numbers nicely (round, commas, %)
- If the question is NOT about data (greeting, meta), output exactly: NO_CODE: <your answer>

You answer with ONLY the Python code — no markdown, no explanations.
"""


class AskRequest(BaseModel):
    session_id: str
    question: str


class AskResponse(BaseModel):
    stdout: str
    stderr: str
    files: list[dict]
    duration_s: float
    attempts: int
    opencode_session_id: str | None = None


def get_preview(filename: str) -> str:
    """Get first 5 rows + columns from a CSV."""
    import pandas as pd
    df = pd.read_csv(UPLOADS / filename)
    cols = list(df.columns)
    preview = df.head(5).to_string(index=False)
    return f"Columns: {cols}\n5 rows:\n{preview}"


def list_session_files() -> list[str]:
    return [f.name for f in sorted(UPLOADS.iterdir()) if f.suffix == ".csv"]


def build_prompt(question: str) -> str:
    files = list_session_files()
    if not files:
        data_desc = "No files uploaded yet."
    else:
        previews = []
        for f in files:
            previews.append(f"--- {f} ---\n{get_preview(f)}")
        data_desc = "\n\n".join(previews)
    print("=== PROMPT ===")
    print(f"Question: {question}")
    print(f"Available files: {files}")
    print(f"Data preview:\n{data_desc}")
    return f"{SYSTEM_PROMPT}\n\nAvailable data:\n{data_desc}\n\nQuestion: {question}"


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


def run_code(code: str) -> dict:
    logger.info("--- EXECUTING CODE ---")
    logger.debug("Code:\n%s", code)

    APP_FILE.write_text(code, encoding="utf-8")
    OUTPUT.mkdir(exist_ok=True)

    start = time.monotonic()
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # Add api/ directory to PYTHONPATH so `import helper` works from working_dir/
        api_dir = str(Path(__file__).parent)
        env["PYTHONPATH"] = api_dir + os.pathsep + env.get("PYTHONPATH", "")

        proc = subprocess.run(
            [sys.executable, "app.py"],
            cwd=str(WORKDIR),
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

    # Auto-heal: move stray output files that landed outside working_dir/output
    stray_dir = Path(__file__).parent / "output"
    if stray_dir.exists() and stray_dir.is_dir():
        for f in stray_dir.iterdir():
            if f.is_file():
                dest = OUTPUT / f.name
                shutil.move(str(f), str(dest))
                logger.warning("Moved stray output file: %s -> %s", f.name, dest)

    files = []
    for f in sorted(OUTPUT.iterdir()):
        if f.is_file():
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


async def get_or_create_session() -> str | None:
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


async def get_history(session_id: str) -> list[dict]:
    """Get message history from OpenCode session."""
    try:
        async with httpx.AsyncClient(base_url=OPENCODE_URL, timeout=30) as client:
            r = await client.get(f"/session/{session_id}/message")
            r.raise_for_status()
            out = []
            for m in r.json() or []:
                role = m.get("info", {}).get("role", "")
                text = "\n".join(p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text").strip()
                if text:
                    out.append({"role": role, "text": text})
            return out
    except Exception:
        return []


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest):
    logger.info("========================================")
    logger.info("NEW REQUEST")
    logger.info("Session ID: %s", req.session_id)
    logger.info("Question: %s", req.question)
    logger.info("========================================")

    session_id = await get_or_create_session()
    if not session_id:
        logger.error("OpenCode server not reachable")
        return AskResponse(stdout="OpenCode server not running. Start it first.", stderr="", files=[], duration_s=0.0, attempts=1, opencode_session_id=None)

    prompt = build_prompt(req.question)
    try:
        reply = await opencode_chat(session_id, prompt)
    except Exception as e:
        logger.error("OpenCode call failed: %s", e)
        return AskResponse(stdout="", stderr=str(e), files=[], duration_s=0.0, attempts=1, opencode_session_id=session_id)

    if "NO_CODE:" in reply:
        answer = reply.split("NO_CODE:", 1)[1].strip()
        logger.info("Conversational reply (no code): %s", answer)
        return AskResponse(stdout=answer, stderr="", files=[], duration_s=0.0, attempts=1, opencode_session_id=session_id)

    code = extract_code(reply)
    if not code:
        # If LLM mentioned saving a file but gave no code, re-prompt for actual code
        keywords = ("saved", "chart", "png", "xlsx", "csv", "output", "graph", "plot")
        if any(kw in reply.lower() for kw in keywords):
            logger.warning("No code extracted but reply mentions output — re-prompting for code")
            fix_prompt = "You mentioned saving output but did not provide Python code. Return ONLY the Python code that generates and saves the output. No explanations."
            try:
                fix_reply = await opencode_chat(session_id, fix_prompt)
                code = extract_code(fix_reply)
            except Exception:
                pass
        if not code:
            logger.warning("No code extracted from reply")
            return AskResponse(stdout=reply.strip(), stderr="", files=[], duration_s=0.0, attempts=1, opencode_session_id=session_id)

    logger.info("Extracted %d chars of code", len(code))
    result = run_code(code)
    attempts = 1

    while result["stderr"].strip() and attempts < MAX_FIX_RETRIES:
        attempts += 1
        logger.info("--- AUTO-FIX attempt %d ---", attempts)
        fix_prompt = f"Your code errored:\n{result['stderr'][-3000:]}\nFix it. Return ONLY the corrected code."
        try:
            fix_reply = await opencode_chat(session_id, fix_prompt)
        except Exception as e:
            logger.error("Fix call failed: %s", e)
            break
        code = extract_code(fix_reply)
        if not code:
            logger.warning("No code in fix reply")
            break
        result = run_code(code)

    logger.info("DONE | attempts=%d | stdout=%d bytes | stderr=%d bytes | files=%d",
                attempts, len(result["stdout"]), len(result["stderr"]), len(result["files"]))

    return AskResponse(
        stdout=result["stdout"],
        stderr=result["stderr"],
        files=result["files"],
        duration_s=result["duration_s"],
        attempts=attempts,
        opencode_session_id=session_id,
    )


@router.get("/outputs")
def list_outputs():
    """List all files in the output folder."""
    # Auto-heal: move stray output files before listing
    stray_dir = Path(__file__).parent / "output"
    if stray_dir.exists() and stray_dir.is_dir():
        OUTPUT.mkdir(exist_ok=True)
        for f in stray_dir.iterdir():
            if f.is_file():
                dest = OUTPUT / f.name
                shutil.move(str(f), str(dest))
                logger.warning("Moved stray output file: %s -> %s", f.name, dest)

    if not OUTPUT.exists():
        return {"files": []}
    files = []
    for f in sorted(OUTPUT.iterdir()):
        if f.is_file():
            data = f.read_bytes()
            files.append({
                "name": f.name,
                "media_type": "image/png" if f.suffix == ".png" else "application/octet-stream",
                "content_base64": base64.b64encode(data).decode(),
            })
    return {"files": files}


@router.get("/history")
async def history(session_id: str):
    logger.info("History request for session: %s", session_id)
    oc_session = await get_or_create_session()
    if not oc_session:
        return {"messages": []}
    msgs = await get_history(oc_session)
    logger.info("Returning %d messages", len(msgs))
    return {"messages": msgs}
