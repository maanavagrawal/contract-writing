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

from . import models, templates as templates_mod
from .db import get_conn, run_migrations
from .extract import extract_fields
from .generate import InvalidMapping, UnknownDocument, fill_document
from .pdf_fill import fill_pdf
from .pdf_render import render_pdf_for_edit
from .schema import (
    EditRequest,
    EditResponse,
    ExtraFieldDTO,
    FieldOverlayDTO,
    GeneratedDoc,
    GeneratedDocFailure,
    GenerateRequest,
    GenerateResponse,
    PageRenderDTO,
    PreviewRequest,
    PreviewResponse,
    TemplateListItem,
    TemplateListResponse,
    TemplateUploadResponse,
    TransactionFields,
)

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Run sqlite migrations on startup. Idempotent — safe to run every boot.
    applied = run_migrations()
    if applied:
        print(f"db: applied migrations: {applied}")
    yield


app = FastAPI(title="Real Estate Paperwork Automator", lifespan=_lifespan)


@app.post("/api/extract")
async def api_extract(
    notes: str = Form(""),
    images: list[UploadFile] = File(default_factory=list),
    # Comma-separated list of template ids whose extra_fields should join
    # the dynamic extraction schema. Pass an empty string (default) to get
    # the original TransactionFields-only behavior. Form fields can't be
    # arrays cleanly so we use a comma-separated string and split server-side.
    active_template_ids: str = Form(""),
) -> dict:
    """Extract structured TransactionFields (and optional template_extras
    from any active uploaded templates) from the agent's notes + MLS images.

    Response shape: TransactionFields fields at the top level, plus an
    optional `template_extras` key when active_template_ids includes any
    template that declares extra_fields. Frontend treats template_extras as
    optional — old clients that don't know about it ignore it cleanly.
    """
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

    # Resolve active templates → their extra_fields. Skip ids we can't find
    # silently (rather than 400ing) so a stale frontend cache doesn't break
    # extraction.
    template_extras: dict[str, list] = {}
    ids = [s.strip() for s in active_template_ids.split(",") if s.strip()]
    if ids:
        with get_conn() as conn:
            for tpl_id in ids:
                tpl = models.get_template(conn, tpl_id)
                if tpl and tpl.extra_fields:
                    template_extras[tpl.id] = tpl.extra_fields

    try:
        result = await extract_fields(
            notes=notes,
            images=image_payloads,
            template_extras=template_extras,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    # The model is either TransactionFields or TransactionFieldsExtended;
    # both are Pydantic, both round-trip cleanly through model_dump.
    return result.model_dump(mode="json")


@app.post("/api/generate", response_model=GenerateResponse)
async def api_generate(req: GenerateRequest) -> GenerateResponse:
    """Fill every requested document. One bad mapping or fill error doesn't
    break the batch — successful docs come back in `documents`, failures in
    `failures`. Frontend can show partial-success UI cleanly.

    The only request-level 400 is "no documents requested." Everything else
    becomes a per-doc failure entry."""
    if not req.documents:
        raise HTTPException(400, "no documents requested")
    out: list[GeneratedDoc] = []
    failures: list[GeneratedDocFailure] = []
    for doc_key in req.documents:
        try:
            out.append(fill_document(doc_key, req.fields, req.agent))
        except UnknownDocument as e:
            failures.append(GeneratedDocFailure(document=doc_key, error=str(e)))
        except InvalidMapping as e:
            failures.append(GeneratedDocFailure(document=doc_key, error=str(e)))
        except FileNotFoundError as e:
            failures.append(GeneratedDocFailure(
                document=doc_key, error=f"template PDF missing: {e}",
            ))
        except OSError as e:
            # Disk full, permission denied, etc. — propagate so the caller
            # knows it's a server-side problem, not bad input.
            raise HTTPException(503, f"could not write generated PDF: {e}")
        except Exception as e:
            # Unexpected fill errors (corrupt PDF, encryption surprise) get
            # captured per-doc so the rest of the batch still ships.
            failures.append(GeneratedDocFailure(
                document=doc_key, error=f"fill failed: {e}",
            ))
    return GenerateResponse(documents=out, failures=failures)


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


@app.get("/api/templates", response_model=TemplateListResponse)
async def api_list_templates() -> TemplateListResponse:
    """List every template the current user can use: their own + IL defaults.
    Light response shape (no mapping JSON, no extras detail) — clients fetch
    the full mapping per-template when they need it."""
    with get_conn() as conn:
        rows = models.list_templates(conn)
    return TemplateListResponse(templates=[
        TemplateListItem(
            id=t.id,
            title=t.title,
            status=t.status,
            is_default=t.is_default,
            created_at=t.created_at,
            extra_field_count=len(t.extra_fields),
        )
        for t in rows
    ])


@app.post("/api/templates/upload", response_model=TemplateUploadResponse)
async def api_upload_template(
    title: str = Form(...),
    pdf: UploadFile = File(...),
) -> TemplateUploadResponse:
    """Accept a PDF, validate, ask GPT to propose a mapping, persist
    everything, and return the proposal so the frontend can open the
    mapping-review UI.

    Sync (per the eng review): users see a spinner; ~30-60s for large
    contracts. Async/polling can come later if real users complain.
    """
    if not pdf.content_type or "pdf" not in pdf.content_type.lower():
        # Be forgiving — Safari sends application/pdf, Chrome sometimes
        # application/octet-stream. Trust the extension as a fallback.
        if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "upload must be a .pdf file")

    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(400, "uploaded file is empty")
    # Reuse the same caps as /api/preview so a giant PDF can't hang the worker.
    _validate_pdf_bytes(pdf_bytes)

    # Validate the PDF has the AcroForm we need.
    try:
        reader = templates_mod.validate_pdf(pdf_bytes)
    except templates_mod.TemplateUploadError as e:
        raise HTTPException(400, str(e))

    # Collect what we hand to the AI: every field + neighbor text.
    field_descs = templates_mod.collect_field_descriptions(reader)
    if not field_descs:
        raise HTTPException(400, "PDF has no fillable fields after parsing")

    template_id = models.new_id()
    pdf_path = templates_mod.save_uploaded_pdf(pdf_bytes, template_id)

    # AI mapping proposal. This is the slow part (~30-60s on Multi-Board).
    try:
        proposal = await templates_mod.propose_mapping(field_descs)
    except templates_mod.AIMappingError as e:
        # Clean up the saved PDF — no point leaving an orphaned file when
        # the mapping never got created.
        try:
            pdf_path.unlink()
        except OSError:
            pass
        raise HTTPException(502, f"AI mapping failed: {e}")

    # Translate proposal into MappingFile + ExtraField list.
    mapping_file, extras = templates_mod.proposal_to_mapping_file(
        proposal,
        title=title,
        source_pdf_filename=f"{template_id}.pdf",
        filled_filename=f"{title.lower().replace(' ', '_')}_filled.pdf",
    )
    mapping_path = templates_mod.write_mapping_file(mapping_file, template_id)

    template_row = templates_mod.build_template_row(
        template_id=template_id,
        title=title,
        source_pdf_path=pdf_path,
        mapping_path=mapping_path,
        extras=extras,
        user_id=models.DEFAULT_USER_ID,
    )
    with get_conn() as conn:
        models.insert_template(conn, template_row)

    return TemplateUploadResponse(
        id=template_id,
        title=title,
        status=template_row.status,
        mapping=mapping_file.model_dump(by_alias=True),
        extra_fields=[
            ExtraFieldDTO(
                name=e.name, type=e.type,
                description=e.description, pdf_field=e.pdf_field,
            )
            for e in extras
        ],
        field_count=len(field_descs),
    )


@app.delete("/api/templates/{template_id}", status_code=204)
async def api_delete_template(template_id: str):
    """Delete a custom template. Defaults are protected by the SQL guard
    in models.delete_template — attempting to delete a default returns
    silently with no rows affected, which we surface as 403."""
    with get_conn() as conn:
        existing = models.get_template(conn, template_id)
        if existing is None:
            raise HTTPException(404, f"template {template_id!r} not found")
        if existing.is_default:
            raise HTTPException(403, "default templates cannot be deleted")
        models.delete_template(conn, template_id)
        # Best-effort filesystem cleanup. Absolute paths (test fixtures) get
        # used as-is; relative paths resolve against repo root.
        repo_root = Path(__file__).resolve().parent.parent
        for path_str in (existing.source_pdf_path, existing.mapping_path):
            p = Path(path_str)
            if not p.is_absolute():
                p = repo_root / p
            try:
                p.unlink()
            except OSError:
                pass
    return Response(status_code=204)


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
