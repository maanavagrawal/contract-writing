"""
FastAPI app — two endpoints + the static frontend.

Run with:
    .venv/bin/uvicorn backend.main:app --reload --port 8000

Then open http://localhost:8000.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

load_dotenv()  # picks up OPENAI_API_KEY from .env

from .extract import extract_fields
from .generate import UnknownDocument, fill_document
from .pdf_fill import fill_pdf
from .pdf_render import render_pdf_for_edit
from .schema import (
    EditRequest,
    EditResponse,
    FieldOverlayDTO,
    GeneratedDoc,
    GenerateRequest,
    GenerateResponse,
    PageRenderDTO,
    PreviewRequest,
    PreviewResponse,
    TransactionFields,
)

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


# Defensive caps to keep a runaway render from hanging the worker. The 4 IL
# templates max out at ~1MB and 15 pages; these limits are 25× headroom.
_MAX_PDF_BYTES = 25 * 1024 * 1024     # 25MB decoded
_MAX_PDF_PAGES = 50


def _validate_pdf_bytes(pdf_bytes: bytes) -> None:
    """Raise HTTPException(413) if the input would blow up the renderer."""
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise HTTPException(413, f"PDF exceeds {_MAX_PDF_BYTES // (1024 * 1024)}MB cap")
    # Cheap page count: parse just enough metadata to know.
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        raise HTTPException(400, f"could not parse PDF: {e}")
    if len(reader.pages) > _MAX_PDF_PAGES:
        raise HTTPException(413, f"PDF exceeds {_MAX_PDF_PAGES}-page cap")


def _build_preview(pdf_bytes: bytes) -> PreviewResponse:
    """Run the PDF through pypdfium2 + pdf_introspect and shape the result for
    the frontend overlay. Single source of truth so /api/preview and the
    preview half of /api/edit stay aligned."""
    pages, fields = render_pdf_for_edit(pdf_bytes)
    return PreviewResponse(
        pages=[
            PageRenderDTO(
                page=p.page,
                width_px=p.width_px,
                height_px=p.height_px,
                image_b64=p.image_b64,
            )
            for p in pages
        ],
        fields=[
            FieldOverlayDTO(
                name=f.name,
                field_type=f.field_type,
                page=f.page,
                rect_px=f.rect_px,
                value=f.value,
                states=f.states,
            )
            for f in fields
        ],
    )


@app.post("/api/preview", response_model=PreviewResponse)
async def api_preview(req: PreviewRequest) -> PreviewResponse:
    try:
        pdf_bytes = base64.b64decode(req.base64_pdf)
    except Exception:
        raise HTTPException(400, "invalid base64_pdf")
    if not pdf_bytes:
        raise HTTPException(400, "empty base64_pdf")
    _validate_pdf_bytes(pdf_bytes)
    try:
        return _build_preview(pdf_bytes)
    except HTTPException:
        raise
    except Exception as e:
        # pypdf / pypdfium2 raise opaque errors on malformed input; return a
        # clean 400 instead of a 500 stack trace.
        raise HTTPException(400, f"could not parse PDF: {e}")


@app.post("/api/edit", response_model=EditResponse)
async def api_edit(req: EditRequest) -> EditResponse:
    """Apply user edits to a generated PDF. We re-walk the AcroForm and write
    /V on every field whose dotted name appears in the edits dict, then return
    a fresh preview so the UI can repaint."""
    try:
        pdf_bytes = base64.b64decode(req.base64_pdf)
    except Exception:
        raise HTTPException(400, "invalid base64_pdf")
    if not pdf_bytes:
        raise HTTPException(400, "empty base64_pdf")
    _validate_pdf_bytes(pdf_bytes)

    from pypdf import PdfReader  # local import keeps top of file tidy
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        edited_bytes = fill_pdf(reader, req.edits)
        preview = _build_preview(edited_bytes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"could not apply edits: {e}")

    return EditResponse(
        document=GeneratedDoc(
            document="edited",
            filename="edited.pdf",
            base64=base64.b64encode(edited_bytes).decode("ascii"),
        ),
        preview=preview,
    )


@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True}


# Static frontend, mounted last so /api/* routes win.
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon() -> Response:
    # Browsers request this on every page load. We don't ship one yet — return
    # 204 No Content so the dev log isn't full of 404 noise.
    return Response(status_code=204)


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
