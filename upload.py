from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from auth import current_user_id, require_chat_owner

router = APIRouter()

WORKDIR = Path(__file__).parent / "working_dir"
UPLOADS_ROOT = WORKDIR / "uploads"


def chat_upload_dir(chat_id: str) -> Path:
    """Per-chat upload folder: working_dir/uploads/{chat_id}/"""
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


@router.post("/{chat_id}")
async def upload(
    chat_id: str,
    files: list[UploadFile] = File(...),
    user_id: str = Depends(current_user_id),
):
    require_chat_owner(chat_id, user_id)
    dest = chat_upload_dir(chat_id)
    saved = []
    for f in files:
        name = Path(f.filename or "").name  # strip any path components
        if not name or not name.lower().endswith(".csv"):
            raise HTTPException(400, "Only .csv files allowed")
        content = await f.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(413, "File too large (max 50MB)")
        (dest / name).write_bytes(content)
        saved.append(name)
    return {"files": saved}


@router.get("/{chat_id}")
def list_files(chat_id: str, user_id: str = Depends(current_user_id)):
    require_chat_owner(chat_id, user_id)
    dest = chat_upload_dir(chat_id)
    files = [f.name for f in sorted(dest.iterdir()) if f.suffix == ".csv"]
    return {"files": files}


@router.get("/{chat_id}/profile/{filename}", response_model=FileProfile)
def profile_file(
    chat_id: str,
    filename: str,
    user_id: str = Depends(current_user_id),
):
    require_chat_owner(chat_id, user_id)
    path = chat_upload_dir(chat_id) / Path(filename).name
    if not path.exists():
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
        name=filename,
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
    path = chat_upload_dir(chat_id) / Path(filename).name
    if path.exists():
        path.unlink()
    return {"deleted": filename}
