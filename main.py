import socket
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from auth import current_user_id
from chat import router as chat_router
from upload import router as upload_router

app = FastAPI(title="AI CSV Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://ai-csv-analyzer.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(
    upload_router,
    prefix="/upload",
    tags=["upload"],
    dependencies=[Depends(current_user_id)],
)
app.include_router(
    chat_router,
    prefix="/chat",
    tags=["chat"],
    dependencies=[Depends(current_user_id)],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root(request: Request):
    """Public reachability check (no auth) — verify nginx -> API with curl."""
    return {
        "service": "ai-csv-api",
        "status": "ok",
        "host": socket.gethostname(),
        "time": datetime.now(timezone.utc).isoformat(),
        "client": request.client.host if request.client else None,
        "x_forwarded_for": request.headers.get("x-forwarded-for"),
        "x_forwarded_proto": request.headers.get("x-forwarded-proto"),
        "host_header": request.headers.get("host"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
