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

import asyncio
import base64
import hashlib
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

load_dotenv()  # picks up env vars from .env in dev; Railway injects via the dashboard

from . import agent_defaults as defaults_mod
from . import auth, models, templates as templates_mod
from .auth import User, current_user
from .db import get_conn, run_migrations
from .extract import extract_fields
from . import generate as generate_mod
from .generate import InvalidMapping, UnknownDocument, fill_document
from .pdf_fill import fill_pdf
from .pdf_render import collect_field_crops, render_pdf_for_edit
from .schema import (
    DefaultPutRequest,
    DefaultsResponse,
    EditRequest,
    EditResponse,
    ExtraFieldDTO,
    FieldOverlayDTO,
    GeneratedDoc,
    GeneratedDocFailure,
    GenerateRequest,
    GenerateResponse,
    MappingCorrection,
    MappingCorrectionsRequest,
    MappingCorrectionsResponse,
    PageRenderDTO,
    PreviewRequest,
    PreviewResponse,
    TemplateListItem,
    TemplateListResponse,
    TemplateUploadResponse,
    TranscribeResponse,
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

# Strong references for fire-and-forget background tasks. Python's
# asyncio.create_task() returns a task that's only weakly referenced by the
# event loop — if no caller holds a strong ref, GC can kill the task before
# it completes (documented behavior of CPython 3.11+). We track every
# fire-and-forget task here and discard after it finishes. Without this,
# the Interfaze shadow log writes silently disappear under load.
_background_tasks: set = set()


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

def _read_dotted(obj: dict, path: str):
    """Walk a dotted path through nested dicts. Returns None on miss. Tiny
    helper used by /api/extract to detect which defaults actually filled a
    previously-empty slot."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _merge_defaults_into(result: dict, defaults: dict[str, str]) -> dict:
    """Merge per-agent defaults into an extraction result.

    Precedence (from plan-eng-review issue 1.4):
        extracted value > saved default > null

    Dotted paths walk nested dicts; intermediate keys are created if missing.
    A default is skipped when the target slot already has a truthy value —
    the user's deal-specific extracted value always wins.
    """
    if not defaults:
        return result
    for path, value in defaults.items():
        parts = path.split(".")
        cur = result
        for part in parts[:-1]:
            existing = cur.get(part)
            if not isinstance(existing, dict):
                cur[part] = {}
            cur = cur[part]
        leaf = parts[-1]
        if cur.get(leaf):
            # Extracted value (or earlier default) already won — don't stomp.
            continue
        cur[leaf] = value
    return result


@app.post("/api/extract")
async def api_extract(
    notes: str = Form(""),
    images: list[UploadFile] = File(default_factory=list),
    active_template_ids: str = Form(""),
    tier: str = Form("full"),
    user: User = Depends(current_user),
) -> dict:
    """Extract structured TransactionFields (and optional template_extras
    from any active uploaded templates) from the agent's notes + MLS images.

    Active template_ids are scoped to the caller's own templates only —
    passing another user's template id silently drops it (template_extras
    becomes empty for that key).

    tier="full" (default) uses gpt-5 and accepts images; tier="live" uses
    gpt-5-mini and ignores images, for cheap debounced typing-triggered
    extractions. Both return the same Pydantic shape.

    Response includes per-agent defaults merged in via the precedence rule
    extracted > default > null. Frontend can distinguish defaults from
    extracted values via the returned `_defaults_applied` list (which
    canonical paths were filled from defaults).
    """
    if not notes.strip() and not images:
        raise HTTPException(400, "must provide notes or at least one image")
    if tier not in ("full", "live"):
        raise HTTPException(400, "tier must be 'full' or 'live'")

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
            tier=tier,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    payload = result.model_dump(mode="json")

    # Server-side defaults merge — agent never sees defaults that don't apply,
    # frontend renders the "filled from default" tick from _defaults_applied.
    with get_conn() as conn:
        defaults = defaults_mod.list_defaults(conn, user.id)
    if defaults:
        before_snapshot = {p: _read_dotted(payload, p) for p in defaults}
        _merge_defaults_into(payload, defaults)
        applied = [
            p for p, before in before_snapshot.items()
            if not before and _read_dotted(payload, p)
        ]
        if applied:
            payload["_defaults_applied"] = applied

    return payload


# Chip strip is anchored on this canonical subset. The order is the order
# chips appear in the UI — most-important first. Anything not in this list
# still extracts (and shows in the accordion), but doesn't get a chip.
# Keep this in sync with frontend/modules/chips.js CHIP_ORDER.
_CHIP_FIELDS: tuple[tuple[str, str], ...] = (
    ("property.address", "Address"),
    ("property.unit",    "Unit"),
    ("transaction_type", "Type"),
    ("purchase_price",   "Price"),
    ("monthly_rent",     "Rent"),
    ("earnest_money",    "Earnest"),
    ("closing_date",     "Closing"),
    ("lease_start",      "Lease start"),
    ("lease_end",        "Lease end"),
    ("tenant_or_buyer_names", "Buyer/Tenant"),
    ("seller_names",     "Seller"),
    ("loan_type",        "Loan"),
    ("loan_rate_type",   "Rate"),
    ("loan_percent_of_price", "LTV %"),
    ("loan_amortization_years", "Term"),
    ("escrowee",         "Escrowee"),
    ("commission_amount", "Commission"),
    ("county",           "County"),
)


def _format_chip_value(v) -> str:
    """Render a chip value for display. Lists join with ' & '; everything
    else is str()'d. Strips whitespace. Empty list → empty string."""
    if v is None:
        return ""
    if isinstance(v, list):
        if not v:
            return ""
        parts = [str(x).strip() for x in v if x]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        if len(parts) == 2:
            return f"{parts[0]} & {parts[1]}"
        return ", ".join(parts)
    return str(v).strip()


@app.post("/api/extract/stream")
async def api_extract_stream(
    notes: str = Form(""),
    images: list[UploadFile] = File(default_factory=list),
    active_template_ids: str = Form(""),
    tier: str = Form("live"),
    user: User = Depends(current_user),
) -> StreamingResponse:
    """Streaming variant of /api/extract. Server-Sent Events; client receives
    one `chip` event per canonical field, then a final `done` event with the
    full payload (so the frontend can also populate the parsed-fields
    accordion).

    Streaming choice (plan-eng-review issue 1.2): fake-stream from server.
    We collect the full Pydantic result first, then iterate through
    _CHIP_FIELDS, emitting events with a 40ms gap so the UI fades chips in
    one-by-one. Trade-off: no perceived latency reduction for the FIRST
    chip vs /api/extract, but every subsequent chip lands in <100ms of the
    one before it — that's the perception we want.
    """
    if not notes.strip() and not images:
        raise HTTPException(400, "must provide notes or at least one image")
    if tier not in ("full", "live"):
        raise HTTPException(400, "tier must be 'full' or 'live'")

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
    with get_conn() as conn:
        if ids:
            for tpl_id in ids:
                tpl = models.get_template(conn, tpl_id, user_id=user.id)
                if tpl and tpl.extra_fields:
                    template_extras[tpl.id] = tpl.extra_fields
        # Per-minute rate limit. Reject before kicking off an OpenAI call —
        # a misbehaving client with a tight retry loop can otherwise burn
        # dollars on gpt-5-mini at the live tier. See agent_defaults.py
        # LIVE_EXTRACT_PER_MINUTE_CAP.
        if not defaults_mod.check_and_record_extract(conn, user.id):
            raise HTTPException(
                429,
                f"live extract rate limit exceeded "
                f"({defaults_mod.LIVE_EXTRACT_PER_MINUTE_CAP}/min)",
            )

    user_id = user.id

    async def event_stream():
        # Initial ping so proxies (Railway, browsers) flush the response head
        # immediately and the client sees the connection is alive. Without
        # this the first 3-8 seconds look identical to a hung request.
        yield "event: started\ndata: {}\n\n"

        try:
            result = await extract_fields(
                notes=notes,
                images=image_payloads,
                template_extras=template_extras,
                tier=tier,
            )
        except RuntimeError as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
            return
        except asyncio.CancelledError:
            # Client closed the SSE connection mid-call. Don't log loudly —
            # the live-extract pipeline aborts frequently when the user types
            # mid-stream. Letting the exception propagate cancels the
            # generator cleanly.
            raise

        payload = result.model_dump(mode="json")

        # IMPORTANT: list_defaults runs AFTER extract_fields (not before).
        # Holding a pool connection across the 3-8s OpenAI call would block
        # other request handlers and exhaust the 10-connection pool under
        # concurrent intake sessions.
        with get_conn() as conn:
            saved_defaults = defaults_mod.list_defaults(conn, user_id)
        defaults_applied: list[str] = []
        if saved_defaults:
            before_snapshot = {p: _read_dotted(payload, p) for p in saved_defaults}
            _merge_defaults_into(payload, saved_defaults)
            defaults_applied = [
                p for p, before in before_snapshot.items()
                if not before and _read_dotted(payload, p)
            ]

        defaults_set = set(defaults_applied)

        # Emit one event per chip-eligible field that has a value. Empty
        # slots are skipped — the chip strip doesn't render placeholder
        # chips, it just shows what we actually have.
        for path, label in _CHIP_FIELDS:
            value = _read_dotted(payload, path)
            display = _format_chip_value(value)
            if not display:
                continue
            chip = {
                "path": path,
                "label": label,
                "value": display,
                "source": "default" if path in defaults_set else "extracted",
            }
            yield f"event: chip\ndata: {json.dumps(chip)}\n\n"
            await asyncio.sleep(0.04)

        # Final done event carries the full payload (canonical + template_extras
        # + _defaults_applied) so the frontend can populate the accordion
        # and not have to re-call /api/extract.
        if defaults_applied:
            payload["_defaults_applied"] = defaults_applied
        yield f"event: done\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # Disable any nginx-style buffering on the way out; Railway's edge
        # proxy honors this hint.
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ---- Per-agent defaults endpoints (workstream C) ----

@app.get("/api/me/defaults", response_model=DefaultsResponse)
async def api_get_defaults(user: User = Depends(current_user)) -> DefaultsResponse:
    """Return the calling agent's saved defaults plus the allow-list of
    eligible paths. Frontend uses the allow-list to render "save as default"
    affordances only where it makes sense."""
    with get_conn() as conn:
        defaults = defaults_mod.list_defaults(conn, user.id)
    return DefaultsResponse(
        defaults=defaults,
        allowed_paths=sorted(defaults_mod.ALLOWED_DEFAULT_PATHS),
    )


@app.put("/api/me/defaults/{field_path:path}")
async def api_put_default(
    field_path: str,
    req: DefaultPutRequest,
    user: User = Depends(current_user),
) -> dict:
    """Save or update one default. Returns 400 for paths outside the
    allow-list; never silently drops (helps the frontend catch typos).
    Always scoped to the caller — there is no admin route that could write
    to another user's row."""
    if not defaults_mod.is_allowed_default_path(field_path):
        raise HTTPException(400, f"field_path not eligible for defaults: {field_path}")
    if not req.value:
        raise HTTPException(400, "value must not be empty (use DELETE to clear)")
    with get_conn() as conn:
        defaults_mod.upsert_default(conn, user.id, field_path, req.value)
    return {"ok": True, "field_path": field_path}


@app.delete("/api/me/defaults/{field_path:path}", status_code=204)
async def api_delete_default(
    field_path: str,
    user: User = Depends(current_user),
) -> Response:
    """Remove one default. Idempotent — deleting a missing row is fine, the
    response is 204 either way. Allow-list-gated to keep the DELETE/PUT
    response shape consistent so the only signal an attacker can extract by
    probing is 'this path is in the allow-list', which is also returned by
    GET /api/me/defaults — no new information leaked."""
    if not defaults_mod.is_allowed_default_path(field_path):
        raise HTTPException(400, f"field_path not eligible for defaults: {field_path}")
    with get_conn() as conn:
        defaults_mod.delete_default(conn, user.id, field_path)
    return Response(status_code=204)


# ---- Voice transcription (workstream B) ----

@app.post("/api/transcribe", response_model=TranscribeResponse)
async def api_transcribe(
    audio: UploadFile = File(...),
    request_id: str = Form(...),
    duration_seconds: int = Form(...),
    user: User = Depends(current_user),
) -> TranscribeResponse:
    """Transcribe a short audio clip via Whisper.

    Hardening (plan-eng-review issue 1.3):
      - Server-side caps: 5 MB body, 100 s audio duration
      - Daily quota: 600 s of audio per agent per day (UTC day boundary)
      - Idempotency: same (user, request_id) within 60 s returns the cached
        transcript without re-billing
      - 502 on Whisper API failures with a clear error body
    """
    if not audio.content_type or not audio.content_type.startswith("audio/"):
        raise HTTPException(400, f"unsupported file type: {audio.content_type}")
    if duration_seconds <= 0 or duration_seconds > defaults_mod.WHISPER_MAX_AUDIO_SECONDS:
        raise HTTPException(
            422,
            f"duration must be 1..{defaults_mod.WHISPER_MAX_AUDIO_SECONDS} seconds",
        )

    body = await audio.read()
    if not body:
        raise HTTPException(400, "audio body was empty")
    if len(body) > defaults_mod.WHISPER_MAX_AUDIO_BYTES:
        raise HTTPException(
            413,
            f"audio exceeds {defaults_mod.WHISPER_MAX_AUDIO_BYTES // (1024 * 1024)} MB limit",
        )

    with get_conn() as conn:
        # Idempotency check first — a retry from a flaky mobile network must
        # not redo the call or re-increment the quota.
        cached = defaults_mod.get_cached_transcript(conn, user.id, request_id)
        if cached is not None:
            seconds_today, _ = defaults_mod.get_today_usage(conn, user.id)
            return TranscribeResponse(
                transcript=cached,
                seconds_used=seconds_today,
                cached=True,
            )

        # Quota check — the daily seconds cap is the cost guard. Reject before
        # making the Whisper call so an over-cap user doesn't pay even once.
        seconds_today, _ = defaults_mod.get_today_usage(conn, user.id)
        if seconds_today + duration_seconds > defaults_mod.WHISPER_DAILY_SECONDS_CAP:
            raise HTTPException(
                429,
                f"daily voice cap reached "
                f"({seconds_today}/{defaults_mod.WHISPER_DAILY_SECONDS_CAP}s used)",
            )

    # The Whisper call runs OUTSIDE the get_conn block — holding a pool
    # connection across an external HTTP call would block other request
    # handlers for the same user.
    try:
        from openai import OpenAI
        import os
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        # Whisper SDK needs a file-like object with a name (it sniffs the
        # extension to pick a decoder). Build one from the upload.
        suffix = (audio.filename or "audio.webm").rsplit(".", 1)[-1] if "." in (audio.filename or "") else "webm"
        file_obj = io.BytesIO(body)
        file_obj.name = f"audio.{suffix}"
        result = await asyncio.to_thread(
            client.audio.transcriptions.create,
            model="whisper-1",
            file=file_obj,
        )
        transcript = (result.text or "").strip()
    except KeyError:
        raise HTTPException(500, "OPENAI_API_KEY not set")
    except Exception as e:
        raise HTTPException(502, f"transcription provider failed: {type(e).__name__}")

    # Record usage + cache the transcript only on success. Both go in one
    # short DB call so the request_id can't end up cached without quota
    # being charged or vice versa.
    with get_conn() as conn:
        with conn.transaction():
            defaults_mod.record_usage(conn, user.id, duration_seconds)
            defaults_mod.cache_transcript(conn, user.id, request_id, transcript)
        new_seconds, _ = defaults_mod.get_today_usage(conn, user.id)

    return TranscribeResponse(
        transcript=transcript,
        seconds_used=new_seconds,
        cached=False,
    )


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
            out.append(fill_document(
                doc_key, req.fields, req.agent,
                template_extras=req.template_extras,
            ))
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


@app.get("/api/admin/interfaze_shadow")
async def api_admin_interfaze_shadow(
    user: User = Depends(current_user),
) -> dict:
    """Last 50 Interfaze shadow comparisons for the calling agent. Useful as
    a quick "is Interfaze actually finding more fields than our CV?" audit
    surface. Scoped to the caller's own uploads — never returns another
    user's data even though this is an admin-style view."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT template_id, cv_field_count, interfaze_field_count,
                   interfaze_latency_ms, interfaze_cost_usd_estimate,
                   interfaze_error, created_at
            FROM interfaze_shadow_log
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (user.id,),
        ).fetchall()
    # Aggregate stats: median diff, total cost, error rate.
    diffs = [int(r[2]) - int(r[1]) for r in rows if r[5] is None]
    total_cost = sum(float(r[4] or 0) for r in rows)
    error_rate = sum(1 for r in rows if r[5] is not None) / max(len(rows), 1)
    return {
        "count": len(rows),
        "median_diff_interfaze_minus_cv": sorted(diffs)[len(diffs) // 2] if diffs else 0,
        "total_shadow_cost_usd": round(total_cost, 4),
        "error_rate": round(error_rate, 3),
        "recent": [
            {
                "template_id": r[0],
                "cv_fields": r[1],
                "interfaze_fields": r[2],
                "latency_ms": r[3],
                "cost_usd": float(r[4] or 0),
                "error": r[5],
                "created_at": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ],
    }


async def _run_interfaze_shadow(
    template_id: str,
    user_id: str,
    pdf_bytes: bytes,
    cv_field_count: int,
    cv_sample: list[dict],
) -> None:
    """Background task: run the Interfaze shadow detection and insert a row
    in interfaze_shadow_log. Never raises into the caller — the upload
    response has already been sent. Errors here are observability events,
    not user-visible failures."""
    try:
        from . import interfaze_shadow
        result = await interfaze_shadow.shadow_detect(pdf_bytes)
        # Field-count diff is a coarse signal — for the "how often does
        # Interfaze find things CV misses?" question we need per-rect overlap
        # analysis, which we defer to an offline script. The counts here are
        # enough to spot whether they're in the same ballpark.
        cv_sample_json = json.dumps(cv_sample, default=str)[:8000]
        interfaze_sample_json = json.dumps(
            [
                {"page": f.page_idx, "label": f.label[:80], "kind": f.kind, "bbox": list(f.bbox)}
                for f in result.fields[:10]
            ],
            default=str,
        )[:8000]
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO interfaze_shadow_log (
                    template_id, user_id,
                    cv_field_count, interfaze_field_count,
                    interfaze_latency_ms, interfaze_cost_usd_estimate,
                    fields_unique_to_cv, fields_unique_to_interfaze,
                    cv_sample_json, interfaze_sample_json,
                    interfaze_error
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    template_id, user_id,
                    cv_field_count, len(result.fields),
                    result.latency_ms, float(result.cost_usd_estimate),
                    # No per-rect overlap yet — leave zero. Offline analysis
                    # can compute this from the sample JSONs.
                    0, 0,
                    cv_sample_json, interfaze_sample_json,
                    result.error,
                ),
            )
        print(
            f"interfaze_shadow: template={template_id} cv={cv_field_count} "
            f"ifz={len(result.fields)} latency={result.latency_ms}ms "
            f"cost=${result.cost_usd_estimate:.4f} err={result.error!r}",
            flush=True,
        )
    except Exception as e:
        # Catch-all: any DB issue, import failure, or unexpected exception
        # must not leak. Worst case we lose one shadow log entry.
        print(f"interfaze_shadow: background task crashed: {type(e).__name__}: {e}", flush=True)


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
        # validate_pdf may have synthesized an AcroForm for a flattened PDF.
        # The bytes returned by validate_pdf are the version we MUST persist
        # to disk — for AcroForm uploads this is the original bytes; for
        # flattened uploads it's the modified bytes with synthetic widgets.
        # If we wrote pdf_bytes (original), fill_pdf later would have no
        # field tree to write /V into.
        reader, persist_bytes = templates_mod.validate_pdf(pdf_bytes)
    except templates_mod.TemplateUploadError as e:
        raise HTTPException(400, str(e))

    # Detect whether validate_pdf went through the field_synth path. The
    # synth-path returns NEW bytes (different object, different content);
    # AcroForm-path returns the same bytes object. Either check works in
    # the current implementation, but `!=` is the durable contract: if a
    # future refactor copies bytes in validate_pdf, identity drifts but
    # content equality survives.
    field_synth_ran = persist_bytes != pdf_bytes

    field_descs = templates_mod.collect_field_descriptions(reader)
    if not field_descs:
        raise HTTPException(400, "PDF has no fillable fields after parsing")

    template_id = models.new_id()
    pdf_path = templates_mod.save_uploaded_pdf(persist_bytes, template_id)

    # Visual crops for fields with no neighbor text — the AI gets a tiny
    # PNG of the area around the field as an extra signal. Without this,
    # 22% of Multi-Board fields are unmappable (no text within the
    # neighbor-radius). See backend/pdf_render.collect_field_crops.
    crops = collect_field_crops(reader, field_descs)

    try:
        # Two-pass: mini classifies all fields fast, gpt-5 escalates the
        # subset mini got wrong or wasn't sure about. Halves upload wall
        # time vs single-pass gpt-5 with comparable accuracy.
        proposal = await templates_mod.propose_mapping_two_pass(field_descs, crops=crops)
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

    # Fire the Interfaze shadow comparison AFTER the user has their response.
    # asyncio.create_task() with no await — the upload handler returns
    # immediately, the shadow runs to completion in the background, logs
    # to interfaze_shadow_log, and quietly drops. Users see zero impact.
    if field_synth_ran:
        from . import interfaze_shadow
        if interfaze_shadow.is_enabled():
            task = asyncio.create_task(_run_interfaze_shadow(
                template_id=template_id,
                user_id=user.id,
                pdf_bytes=pdf_bytes,
                cv_field_count=len(field_descs),
                cv_sample=field_descs[:10],
            ))
            # Hold a strong reference until the task completes — otherwise
            # Python's GC can collect it mid-flight (CPython 3.11+
            # documented behavior of asyncio.create_task).
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

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


@app.patch("/api/templates/{template_id}/mapping", response_model=MappingCorrectionsResponse)
async def api_patch_template_mapping(
    template_id: str,
    req: MappingCorrectionsRequest,
    user: User = Depends(current_user),
) -> MappingCorrectionsResponse:
    """Apply a batch of user-supplied corrections to a template's mapping.

    Used by the low-confidence review UI: for each AI-flagged uncertain
    field, the user decides whether to accept the AI's guess (no PATCH
    needed), override to a canonical path or extra_field, or skip (leave
    blank). The frontend collects every correction and submits one PATCH.

    Atomic on disk: we build the full new mapping in memory and write
    once at the end. A bad canonical_path mid-batch rejects the whole
    request — better than half-applying corrections and leaving the user
    with an inconsistent mapping. The template's DB status drops to
    "ready" when low_confidence is empty after the patch.
    """
    if not req.corrections:
        raise HTTPException(400, "no corrections supplied")

    # Ownership + existence check, same shape as the DELETE endpoint.
    with get_conn() as conn:
        existing = models.get_template(conn, template_id, user_id=user.id)
        if existing is None:
            raise HTTPException(404, f"template {template_id!r} not found")

    # Load the mapping JSON via the same path the generate flow uses, so the
    # validation pass that catches malformed JSON applies here too.
    try:
        mapping = generate_mod._load_mapping(template_id)
    except generate_mod.UnknownDocument:
        raise HTTPException(404, f"mapping for {template_id!r} not found on disk")
    except generate_mod.InvalidMapping as e:
        raise HTTPException(500, f"existing mapping is invalid: {e}")

    # Index existing low_confidence so corrections can be matched + removed.
    lc_by_field: dict[str, dict] = {
        lc["pdf_field"]: lc for lc in (mapping.low_confidence or [])
        if isinstance(lc, dict) and "pdf_field" in lc
    }

    # Re-shape existing extras (list[dict]) for upsert-by-name.
    extras_by_name: dict[str, dict] = {
        e.get("name"): e for e in (mapping.extra_fields or [])
        if isinstance(e, dict) and e.get("name")
    }

    fields = dict(mapping.fields)
    new_low_confidence: list[dict] = list(mapping.low_confidence or [])

    # Validate every correction BEFORE mutating anything. We collect errors
    # so the user sees all problems at once instead of fixing them one PATCH
    # at a time.
    errors: list[str] = []
    for c in req.corrections:
        if c.pdf_field not in fields:
            errors.append(f"unknown pdf_field {c.pdf_field!r}")
            continue
        chose = sum(1 for x in (c.canonical_path, c.extra_field_name) if x) + (1 if c.skip else 0)
        if chose != 1:
            errors.append(
                f"{c.pdf_field}: exactly one of canonical_path / extra_field_name / skip must be set"
            )
            continue
        if c.canonical_path and c.canonical_path not in templates_mod._CANONICAL_PATH_ALLOWLIST:
            errors.append(f"{c.pdf_field}: unknown canonical_path {c.canonical_path!r}")
            continue
        if c.extra_field_name:
            ftype = (c.extra_field_type or "text").lower()
            if ftype not in {"text", "money", "date", "number", "bool", "list_str"}:
                errors.append(f"{c.pdf_field}: invalid extra_field_type {c.extra_field_type!r}")
                continue
    if errors:
        raise HTTPException(400, {"errors": errors})

    # Apply: rewrite fields[pdf_field], remove from low_confidence, upsert
    # extras when needed.
    for c in req.corrections:
        if c.canonical_path:
            fields[c.pdf_field] = "{" + c.canonical_path + "}"
        elif c.extra_field_name:
            fields[c.pdf_field] = "{template_extras." + c.extra_field_name + "}"
            extras_by_name[c.extra_field_name] = {
                "name": c.extra_field_name,
                "type": (c.extra_field_type or "text").lower(),
                "description": (c.extra_field_description or "").strip(),
                "pdf_field": c.pdf_field,
            }
        else:  # skip
            fields[c.pdf_field] = ""
        new_low_confidence = [
            lc for lc in new_low_confidence
            if not (isinstance(lc, dict) and lc.get("pdf_field") == c.pdf_field)
        ]

    # Persist the updated mapping. Build a fresh MappingFile so Pydantic
    # validates the new shape before we hit the disk.
    from .schema import MappingFile, MappingMeta
    new_mapping = MappingFile(
        meta=MappingMeta(
            title=mapping.meta.title,
            source_pdf=mapping.meta.source_pdf,
            filled_filename=mapping.meta.filled_filename,
            notes=mapping.meta.notes,
        ),
        fields=fields,
        extra_fields=list(extras_by_name.values()),
        low_confidence=new_low_confidence,
    )
    templates_mod.write_mapping_file(new_mapping, template_id)

    # Update DB row: clear needs_attention status when the banner is now empty.
    new_status = "ready" if not new_low_confidence else existing.status
    if new_status != existing.status:
        with get_conn() as conn:
            models.update_template_status(conn, template_id, user_id=user.id, status=new_status)

    return MappingCorrectionsResponse(
        mapping=new_mapping.model_dump(by_alias=True),
        extra_fields=list(extras_by_name.values()),
        low_confidence_remaining=len(new_low_confidence),
        status=new_status,
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
