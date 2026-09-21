from pathlib import Path

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter()

UPLOAD_DIR = Path(__file__).parent / "working_dir" / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


class FileProfile(BaseModel):
    name: str
    rows: int
    columns: list[str]
    column_types: dict[str, str]
    missing_values: dict[str, int]
    numeric_stats: dict[str, dict[str, float]]
    preview: list[dict]


@router.post("/{session_id}")
async def upload(session_id: str, files: list[UploadFile] = File(...)):
    saved = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".csv"):
            raise HTTPException(400, "Only .csv files allowed")
        content = await f.read()
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(413, "File too large (max 50MB)")
        (UPLOAD_DIR / f.filename).write_bytes(content)
        saved.append(f.filename)
    return {"files": saved}


@router.get("/{session_id}")
def list_files(session_id: str):
    files = [f.name for f in sorted(UPLOAD_DIR.iterdir()) if f.suffix == ".csv"]
    return {"files": files}


@router.get("/{session_id}/profile/{filename}", response_model=FileProfile)
def profile_file(session_id: str, filename: str):
    path = UPLOAD_DIR / filename
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


@router.delete("/{session_id}/{filename}")
def delete_file(session_id: str, filename: str):
    path = UPLOAD_DIR / filename
    if path.exists():
        path.unlink()
    return {"deleted": filename}
