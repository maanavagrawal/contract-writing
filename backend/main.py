"""
FastAPI app — auth + extract/generate/preview/edit + the static frontend.

Run with:
    .venv/bin/uvicorn backend.main:app --reload --port 8000

Then open http://localhost:8000.

Multi-tenant: every /api/* route requires a session cookie. user_id is
threaded through every read and write so a request from User A can never
touch User B's templates or transactions.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

load_dotenv()  # picks up env vars from .env in dev; Railway injects via the dashboard

from . import auth, models, templates as templates_mod
from .auth import User, current_user
from .db import get_conn, run_migrations
from .extract import extract_fields
from .generate import InvalidMapping, UnknownDocument, fill_document
from .pdf_fill import fill_pdf
from .pdf_render import collect_field_crops, render_pdf_for_edit
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
)

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Run Postgres migrations on startup. Idempotent — safe to run every boot.
    applied = run_migrations()
    if applied:
        print(f"db: applied migrations: {applied}")
    yield


app = FastAPI(title="memoir", lifespan=_lifespan)


# ---------------------- AUTH ROUTES ----------------------

@app.post("/api/auth/login")
async def api_auth_login(
    request: Request,
    payload: dict = Body(...),
):
    """Send a magic link to the email in the body. Always returns 200 to
    prevent email enumeration — the client message is generic regardless of
    whether the email is known."""
    email = (payload.get("email") or "").strip()
    if not email:
        raise HTTPException(400, "email is required")

    base_url = auth._resolve_base_url(request)
    try:
        with get_conn() as conn:
            auth.send_magic_link(conn, email, base_url)
    except auth.RateLimitError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        # Don't leak internals; log and return generic.
        print(f"auth.login: unexpected error: {e}")
        raise HTTPException(503, "could not send login email")

    return {"ok": True}


@app.get("/auth/redeem", response_class=HTMLResponse)
async def auth_redeem_page(token: str):
    """Two-step redemption: GET shows a page with a button that POSTs the
    redemption. Defeats email-prefetchers (Outlook Safe Links, Gmail's
    image proxy, Slack unfurls) that would otherwise burn the single-use
    token before the user clicks it.

    The token is embedded as a hidden input. The form POSTs to
    /api/auth/redeem which returns the session cookie + redirect.
    """
    safe_token = token.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Log in to memoir</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0d1117; color: #f0f6fc; display: grid; place-items: center;
           min-height: 100vh; margin: 0; }}
    .card {{ background: #161b22; padding: 32px 40px; border-radius: 12px;
           border: 1px solid #30363d; max-width: 380px; text-align: center; }}
    h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 8px; }}
    p {{ color: #8b949e; font-size: 14px; line-height: 1.5; margin: 0 0 20px; }}
    button {{ background: #1f6feb; color: white; border: 0; padding: 10px 20px;
              font-size: 14px; font-weight: 500; border-radius: 6px;
              cursor: pointer; width: 100%; }}
    button:hover {{ background: #388bfd; }}
    button:disabled {{ background: #30363d; cursor: wait; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Log in to memoir</h1>
    <p>Click the button below to complete your login.</p>
    <form id="f" method="POST" action="/api/auth/redeem">
      <input type="hidden" name="token" value="{safe_token}">
      <button type="submit" id="btn">Log in</button>
    </form>
  </div>
  <script>
    document.getElementById('f').addEventListener('submit', function(e) {{
      e.preventDefault();
      var btn = document.getElementById('btn');
      btn.disabled = true; btn.textContent = 'Logging in…';
      var fd = new FormData(e.target);
      fetch('/api/auth/redeem', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
        body: new URLSearchParams(fd),
      }}).then(function(r) {{
        if (r.ok) {{ window.location.href = '/'; }}
        else {{ btn.disabled = false; btn.textContent = 'Try again';
                r.text().then(function(t) {{ alert(t || 'Login failed'); }}); }}
      }}).catch(function() {{
        btn.disabled = false; btn.textContent = 'Try again';
        alert('Network error');
      }});
    }});
  </script>
</body>
</html>"""


@app.post("/api/auth/redeem")
async def api_auth_redeem(
    request: Request,
    response: Response,
    token: str = Form(...),
):
    """Burn the magic-link token, issue a session, and set the cookie."""
    with get_conn() as conn:
        user = auth.redeem_token(conn, token)
        plaintext = auth.issue_session(conn, user)
    auth.set_session_cookie(response, plaintext, secure=auth._is_secure_request(request))
    return {"ok": True, "email": user.email}


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request, response: Response):
    """Delete the session row + clear the cookie. Idempotent — calling logout
    when not logged in is a no-op."""
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    if cookie:
        with get_conn() as conn:
            auth.revoke_session(conn, cookie)
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
async def api_auth_me(user: User = Depends(current_user)):
    """Cheap auth probe; frontend hits this on boot to decide login-vs-app."""
    return {"id": user.id, "email": user.email}


# ---------------------- APP ROUTES ----------------------

@app.post("/api/extract")
async def api_extract(
    notes: str = Form(""),
    images: list[UploadFile] = File(default_factory=list),
    active_template_ids: str = Form(""),
    user: User = Depends(current_user),
) -> dict:
    """Extract structured TransactionFields (and optional template_extras
    from any active uploaded templates) from the agent's notes + MLS images.

    Active template_ids are scoped to the caller's own templates only —
    passing another user's template id silently drops it (template_extras
    becomes empty for that key).
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

    template_extras: dict[str, list] = {}
    ids = [s.strip() for s in active_template_ids.split(",") if s.strip()]
    if ids:
        with get_conn() as conn:
            for tpl_id in ids:
                tpl = models.get_template(conn, tpl_id, user_id=user.id)
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

    return result.model_dump(mode="json")


@app.post("/api/generate", response_model=GenerateResponse)
async def api_generate(
    req: GenerateRequest,
    user: User = Depends(current_user),
) -> GenerateResponse:
    """Fill every requested document. user_id-scoped: document keys must
    correspond to templates owned by the caller. Unknown / unowned keys
    become per-doc failures so partial-success UX still works."""
    if not req.documents:
        raise HTTPException(400, "no documents requested")

    with get_conn() as conn:
        owned_ids = {
            t.id for t in models.list_templates(conn, user_id=user.id)
        }

    out: list[GeneratedDoc] = []
    failures: list[GeneratedDocFailure] = []
    for doc_key in req.documents:
        if doc_key not in owned_ids:
            failures.append(GeneratedDocFailure(
                document=doc_key, error=f"template '{doc_key}' not found",
            ))
            continue
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
            raise HTTPException(503, f"could not write generated PDF: {e}")
        except Exception as e:
            failures.append(GeneratedDocFailure(
                document=doc_key, error=f"fill failed: {e}",
            ))
    return GenerateResponse(documents=out, failures=failures)


# Defensive caps to keep a runaway render from hanging the worker.
_MAX_PDF_BYTES = 25 * 1024 * 1024
_MAX_PDF_PAGES = 50


def _validate_pdf_bytes(pdf_bytes: bytes) -> None:
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise HTTPException(413, f"PDF exceeds {_MAX_PDF_BYTES // (1024 * 1024)}MB cap")
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        raise HTTPException(400, f"could not parse PDF: {e}")
    if len(reader.pages) > _MAX_PDF_PAGES:
        raise HTTPException(413, f"PDF exceeds {_MAX_PDF_PAGES}-page cap")


def _build_preview(pdf_bytes: bytes) -> PreviewResponse:
    pages, fields = render_pdf_for_edit(pdf_bytes)
    return PreviewResponse(
        pages=[
            PageRenderDTO(page=p.page, width_px=p.width_px, height_px=p.height_px, image_b64=p.image_b64)
            for p in pages
        ],
        fields=[
            FieldOverlayDTO(name=f.name, field_type=f.field_type, page=f.page,
                            rect_px=f.rect_px, value=f.value, states=f.states)
            for f in fields
        ],
    )


@app.post("/api/preview", response_model=PreviewResponse)
async def api_preview(
    req: PreviewRequest,
    user: User = Depends(current_user),
) -> PreviewResponse:
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
        raise HTTPException(400, f"could not parse PDF: {e}")


@app.post("/api/edit", response_model=EditResponse)
async def api_edit(
    req: EditRequest,
    user: User = Depends(current_user),
) -> EditResponse:
    try:
        pdf_bytes = base64.b64decode(req.base64_pdf)
    except Exception:
        raise HTTPException(400, "invalid base64_pdf")
    if not pdf_bytes:
        raise HTTPException(400, "empty base64_pdf")
    _validate_pdf_bytes(pdf_bytes)

    from pypdf import PdfReader
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
    """Unauthenticated health probe. Used by uptime checks; doesn't leak any
    user state."""
    return {"ok": True}


@app.get("/api/templates", response_model=TemplateListResponse)
async def api_list_templates(
    user: User = Depends(current_user),
) -> TemplateListResponse:
    """List the caller's own templates. No shared defaults; each user uploads
    their own."""
    with get_conn() as conn:
        rows = models.list_templates(conn, user_id=user.id)
    return TemplateListResponse(templates=[
        TemplateListItem(
            id=t.id,
            title=t.title,
            status=t.status,
            is_default=False,  # legacy field, always False in multi-tenant mode
            created_at=t.created_at,
            extra_field_count=len(t.extra_fields),
        )
        for t in rows
    ])


@app.post("/api/templates/upload", response_model=TemplateUploadResponse)
async def api_upload_template(
    title: str = Form(...),
    pdf: UploadFile = File(...),
    user: User = Depends(current_user),
) -> TemplateUploadResponse:
    """Accept a PDF, validate, ask GPT to propose a mapping, persist
    everything under the caller's user_id."""
    if not pdf.content_type or "pdf" not in pdf.content_type.lower():
        if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "upload must be a .pdf file")

    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(400, "uploaded file is empty")
    _validate_pdf_bytes(pdf_bytes)

    # Cache lookup: same user uploading the same PDF bytes hits a prior
    # mapping and skips the 60-90s AI call. Cross-user hits don't apply —
    # Alice's mapping is private to Alice (even though it would be byte-
    # identical, persisting that link would be a privacy footgun).
    #
    # Title handling: if the user typed a different title than the cached
    # row's, we UPDATE the cached row to the new title rather than silently
    # dropping the user's input. The mapping itself is byte-identical so
    # there's no semantic confusion — it's just "I want to call this 'Lease
    # Pet Addendum' now instead of 'Pet Addendum'."
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    with get_conn() as conn:
        cached = models.find_template_by_pdf_sha(conn, pdf_sha, user_id=user.id)
    if cached is not None:
        prior_mapping_path = Path(cached.mapping_path)
        if not prior_mapping_path.is_absolute():
            prior_mapping_path = ROOT / prior_mapping_path
        try:
            prior_mapping = json.loads(prior_mapping_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            # Cache miss in practice — fall through to fresh mapping.
            print(f"upload: cache hit but mapping file unreadable ({e}); refreshing")
            cached = None
        else:
            # Update title on the cached row if the user provided a different
            # one. Also update the mapping JSON's _meta.title so generated
            # filenames + downloaded PDFs reflect the new label.
            effective_title = cached.title
            if title and title != cached.title:
                with get_conn() as conn:
                    models.update_template_title(conn, cached.id, user.id, title)
                effective_title = title
                # Mirror the title change into the mapping JSON's _meta block.
                meta = prior_mapping.setdefault("_meta", {})
                meta["title"] = title
                try:
                    prior_mapping_path.write_text(json.dumps(prior_mapping, indent=2))
                except OSError as e:
                    # Non-fatal — DB row title is authoritative; mapping file
                    # title is cosmetic for filenames. Log and continue.
                    print(f"upload: could not rewrite mapping title ({e})")

            return TemplateUploadResponse(
                id=cached.id,
                title=effective_title,
                status=cached.status,
                mapping=prior_mapping,
                extra_fields=[
                    ExtraFieldDTO(name=e.name, type=e.type,
                                  description=e.description, pdf_field=e.pdf_field)
                    for e in cached.extra_fields
                ],
                field_count=len(prior_mapping.get("fields", {})),
            )

    try:
        reader = templates_mod.validate_pdf(pdf_bytes)
    except templates_mod.TemplateUploadError as e:
        raise HTTPException(400, str(e))

    field_descs = templates_mod.collect_field_descriptions(reader)
    if not field_descs:
        raise HTTPException(400, "PDF has no fillable fields after parsing")

    template_id = models.new_id()
    pdf_path = templates_mod.save_uploaded_pdf(pdf_bytes, template_id)

    # Visual crops for fields with no neighbor text — the AI gets a tiny
    # PNG of the area around the field as an extra signal. Without this,
    # 22% of Multi-Board fields are unmappable (no text within the
    # neighbor-radius). See backend/pdf_render.collect_field_crops.
    crops = collect_field_crops(reader, field_descs)

    try:
        proposal = await templates_mod.propose_mapping(field_descs, crops=crops)
    except templates_mod.AIMappingError as e:
        try:
            pdf_path.unlink()
        except OSError:
            pass
        raise HTTPException(502, f"AI mapping failed: {e}")

    mapping_file, extras, unknown_paths, low_confidence, btn_warnings = templates_mod.proposal_to_mapping_file(
        proposal,
        title=title,
        source_pdf_filename=f"{template_id}.pdf",
        filled_filename=f"{title.lower().replace(' ', '_')}_filled.pdf",
        field_descriptions=field_descs,
    )

    # Structural validation: catch the worst mapping failures before they
    # reach a paying customer. Auto-flag needs_attention when too many
    # fields are unmapped or hallucinated. btn_warnings surfaces /Btn proposals
    # whose state names or canonical-enum values were pruned (per F4/F10
    # outside-voice findings, 2026-05-10 review).
    validation_warnings = templates_mod.validate_mapping_structure(
        mapping_file, field_descs, unknown_paths, low_confidence, btn_warnings,
    )
    initial_status = "needs_attention" if validation_warnings else "ready"

    mapping_path = templates_mod.write_mapping_file(mapping_file, template_id)

    template_row = templates_mod.build_template_row(
        template_id=template_id,
        title=title,
        source_pdf_path=pdf_path,
        mapping_path=mapping_path,
        extras=extras,
        user_id=user.id,
        pdf_sha256=pdf_sha,
        status=initial_status,
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
        warnings=validation_warnings,
    )


@app.delete("/api/templates/{template_id}", status_code=204)
async def api_delete_template(
    template_id: str,
    user: User = Depends(current_user),
):
    """Delete the caller's own template. Returns 404 if the id doesn't exist
    OR isn't owned by the caller — never 403, so attackers can't enumerate
    other users' template ids."""
    with get_conn() as conn:
        existing = models.get_template(conn, template_id, user_id=user.id)
        if existing is None:
            raise HTTPException(404, f"template {template_id!r} not found")
        rowcount = models.delete_template(conn, template_id, user_id=user.id)
        if rowcount == 0:
            return Response(status_code=204)
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


# ---------------------- STATIC + SHELL ROUTES ----------------------

@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
async def root() -> FileResponse:
    """Serve the app shell. Auth state is checked client-side via /api/auth/me;
    if not logged in, the JS redirects to /login."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "login.html"))


@app.get("/{path:path}")
async def static_passthrough(path: str) -> FileResponse:
    """Serve any frontend file by name (styles.css, app.js, login.js, etc.).

    Path-traversal guard: resolve the absolute path and reject anything that
    isn't strictly inside FRONTEND_DIR. Without this, requests like
    GET /%2E%2E/backend/auth.py escape the frontend dir and exfiltrate
    server-side source. The starlette router URL-decodes path params before
    dispatch, so %2E%2E arrives as `..` in this handler — the relative_to
    check below is what actually stops the traversal.
    """
    if path.startswith("api/") or path.startswith("auth/"):
        raise HTTPException(404)
    frontend_root = FRONTEND_DIR.resolve()
    try:
        target = (FRONTEND_DIR / path).resolve()
        target.relative_to(frontend_root)
    except (ValueError, OSError):
        # ValueError: target escaped FRONTEND_DIR. OSError: too many symlinks
        # or other resolution failure. Either way, refuse.
        raise HTTPException(404)
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    return FileResponse(str(target))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
