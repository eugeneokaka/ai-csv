from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

app.include_router(upload_router, prefix="/upload", tags=["upload"])
app.include_router(chat_router, prefix="/chat", tags=["chat"])


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
