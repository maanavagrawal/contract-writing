"""
FastAPI app — two endpoints + the static frontend.

Run with:
    .venv/bin/uvicorn backend.main:app --reload --port 8000

Then open http://localhost:8000.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()  # picks up OPENAI_API_KEY from .env

from .extract import extract_fields
from .generate import UnknownDocument, fill_document
from .schema import GenerateRequest, GenerateResponse, TransactionFields

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="Real Estate Paperwork Automator")


@app.post("/api/extract", response_model=TransactionFields)
async def api_extract(
    notes: str = Form(""),
    images: list[UploadFile] = File(default_factory=list),
) -> TransactionFields:
    if not notes.strip() and not images:
        raise HTTPException(400, "must provide notes or at least one image")

    image_payloads: list[tuple[bytes, str]] = []
    for upload in images:
        if not upload.content_type or not upload.content_type.startswith("image/"):
            raise HTTPException(400, f"unsupported file type: {upload.content_type}")
        content = await upload.read()
        if not content:
            continue
        image_payloads.append((content, upload.content_type))

    try:
        return await extract_fields(notes=notes, images=image_payloads)
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/api/generate", response_model=GenerateResponse)
async def api_generate(req: GenerateRequest) -> GenerateResponse:
    if not req.documents:
        raise HTTPException(400, "no documents requested")
    out = []
    for doc_key in req.documents:
        try:
            out.append(fill_document(doc_key, req.fields, req.agent))
        except UnknownDocument as e:
            raise HTTPException(400, str(e))
    return GenerateResponse(documents=out)


@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True}


# Static frontend, mounted last so /api/* routes win.
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/{path:path}")
async def static_passthrough(path: str) -> FileResponse:
    """Serve any frontend file by name (styles.css, app.js, etc.)."""
    if path.startswith("api/"):
        raise HTTPException(404)
    target = FRONTEND_DIR / path
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    return FileResponse(str(target))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
