"""
Interfaze shadow comparison for the field-synthesis path.

When `field_synth.try_synthesize` runs (only on flattened PDFs without an
AcroForm), we ALSO ask Interfaze to detect fields on the same pages — but
only if INTERFAZE_API_KEY is set and INTERFAZE_SHADOW_ENABLED=true. The
results never affect what the user sees. They land in `interfaze_shadow_log`
so we can compare offline whether Interfaze beats our CV pipeline on real
uploads.

Why shadow mode rather than A/B:
  - Users get a stable, predictable experience (the CV pipeline they're
    already using)
  - Free credits cover the experiment; we never gamble user latency on a
    beta vendor
  - After a week of real uploads we have grounded data to make the
    "promote Interfaze to primary" decision (or not)

# Shape
  shadow_detect(pdf_bytes) -> ShadowResult
     │
     ├── Render each page to PNG (200 DPI, same as CV path)
     ├── For each page, call Interfaze /v1/chat/completions with a
     │   structured-output prompt: "List every form field's bbox + label"
     └── Aggregate results into a flat list of InterfazeField

# Failure mode
Any error (network, auth, malformed response) logs the failure to the
shadow row and returns (None, error_text). NEVER raises into the upload
caller. The user's experience is untouched.

# Cost guard
Per-call timeout of 30s. Capped at 14 pages per shadow (matches max contract
length). Free credit usage tracked via the cost_usd_estimate column.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import time
from dataclasses import dataclass, field

import pypdfium2
from pydantic import BaseModel, Field

# Lazy import — only needed when the shadow actually runs.
# from openai import OpenAI

# Same render DPI as field_synth so we're comparing like to like.
SHADOW_RENDER_DPI = 200
SHADOW_SCALE = SHADOW_RENDER_DPI / 72.0
SHADOW_MAX_PAGES = 14    # CAR contracts top out around this
SHADOW_TIMEOUT_S = 30.0  # per-page upper bound

# Interfaze pricing (May 2026, from interfaze.ai/pricing):
#   $1.50 / MTok input
#   $3.50 / MTok output
# Rough estimate: each PNG-page request ~3K input tokens (image) + 500 output.
_PRICE_INPUT_USD_PER_MTOK = 1.50
_PRICE_OUTPUT_USD_PER_MTOK = 3.50


class _IfzField(BaseModel):
    """One field as Interfaze sees it. Schema is intentionally close to
    SynthField so the diff is meaningful."""
    label: str = Field(..., description="The visible label next to this field, e.g. 'Buyer Name'.")
    bbox: list[float] = Field(..., description="Bounding box [x0,y0,x1,y1] in image pixel coords (Y top-down).")
    kind: str = Field(..., description="'text' for underlines, 'checkbox' for square boxes.")


class _IfzPageResult(BaseModel):
    fields: list[_IfzField] = Field(default_factory=list)


@dataclass
class InterfazeField:
    page_idx: int
    label: str
    bbox: tuple[float, float, float, float]  # image-pixel coords, Y top-down
    kind: str


@dataclass
class ShadowResult:
    """What we logged. n_pages_scanned can be less than total if SHADOW_MAX_PAGES
    capped us. error is set when ANY page failed (we still return the ones
    that succeeded, so a partial result is useful)."""
    fields: list[InterfazeField] = field(default_factory=list)
    n_pages_scanned: int = 0
    latency_ms: int = 0
    cost_usd_estimate: float = 0.0
    error: str | None = None


def is_enabled() -> bool:
    """Cheap env check. If false, shadow_detect is a no-op."""
    return (
        os.environ.get("INTERFAZE_SHADOW_ENABLED", "").lower() in ("1", "true", "yes")
        and bool(os.environ.get("INTERFAZE_API_KEY"))
    )


def _render_page_png_b64(pdf_bytes: bytes, page_idx: int) -> str:
    """Render one page to a base64 PNG data URI. Same DPI as the CV pipeline
    so any per-page diff between CV and Interfaze can't be blamed on render
    resolution mismatch."""
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        page = pdf[page_idx]
        bitmap = page.render(scale=SHADOW_SCALE, rotation=0)
        pil = bitmap.to_pil()
        page.close()
    finally:
        pdf.close()
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


_FIELD_EXTRACTION_PROMPT = """\
You are looking at one page of a real-estate contract PDF rendered to image.
List EVERY blank form field a user would need to fill in. Two kinds:
  - 'text': horizontal underlines or boxed text areas where someone writes a name, date, price, etc.
  - 'checkbox': small square outlines (typically 12-20 pixels) to be checked.

For each field, output: the visible label next to it (or "" if no label), the
bounding box as [x0, y0, x1, y1] in image pixel coordinates with Y going DOWN
from top-left, and the kind. Be exhaustive — missing fields is worse than
extra ones (we'll filter false positives downstream).

Skip pre-filled values. Skip body paragraphs. Skip page numbers. ONLY list
blanks that a real human would actually need to write into.
"""


async def shadow_detect(pdf_bytes: bytes) -> ShadowResult:
    """Run Interfaze's field detection on a flattened PDF, page by page.

    Never raises — any failure becomes ShadowResult(error=...) and the upload
    pipeline continues unaffected. Callers check `.error` and `.fields` to
    decide what to log.
    """
    if not is_enabled():
        return ShadowResult(error="shadow disabled (INTERFAZE_SHADOW_ENABLED=false or no API key)")

    try:
        from openai import OpenAI
    except ImportError as e:
        return ShadowResult(error=f"openai SDK not installed: {e}")

    client = OpenAI(
        api_key=os.environ["INTERFAZE_API_KEY"],
        base_url=os.environ.get("INTERFAZE_BASE_URL", "https://api.interfaze.ai/v1"),
        timeout=SHADOW_TIMEOUT_S,
    )

    # Count pages once. pypdfium2 reads page count cheaply.
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    n_total = len(pdf)
    pdf.close()
    n_to_scan = min(n_total, SHADOW_MAX_PAGES)

    result = ShadowResult(n_pages_scanned=n_to_scan)
    t0 = time.time()
    errors: list[str] = []

    schema = _IfzPageResult.model_json_schema()

    for page_idx in range(n_to_scan):
        try:
            png_b64 = _render_page_png_b64(pdf_bytes, page_idx)
        except Exception as e:
            errors.append(f"render p{page_idx}: {type(e).__name__}: {e}")
            continue

        try:
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model=os.environ.get("INTERFAZE_MODEL", "interfaze-beta"),
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _FIELD_EXTRACTION_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{png_b64}"},
                            },
                        ],
                    }
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "PageFields",
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
        except Exception as e:
            # Network error, auth failure, model unavailable, structured-output
            # rejection — all become a per-page error. Other pages can still
            # succeed. We log the first error verbatim so the operator can
            # debug without combing through every page.
            errors.append(f"call p{page_idx}: {type(e).__name__}: {e}")
            continue

        # Token accounting for the cost estimate.
        usage = getattr(resp, "usage", None)
        if usage is not None:
            in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
            out_tok = int(getattr(usage, "completion_tokens", 0) or 0)
            result.cost_usd_estimate += (
                in_tok * _PRICE_INPUT_USD_PER_MTOK / 1_000_000.0
                + out_tok * _PRICE_OUTPUT_USD_PER_MTOK / 1_000_000.0
            )

        # Parse the structured response.
        raw = resp.choices[0].message.content or "{}"
        try:
            parsed = _IfzPageResult.model_validate_json(raw)
        except Exception as e:
            errors.append(f"parse p{page_idx}: {type(e).__name__}: {e}")
            continue

        for f in parsed.fields:
            if len(f.bbox) != 4:
                continue  # malformed bbox; skip
            result.fields.append(InterfazeField(
                page_idx=page_idx,
                label=f.label,
                bbox=tuple(float(v) for v in f.bbox),
                kind=f.kind if f.kind in ("text", "checkbox") else "text",
            ))

    result.latency_ms = int((time.time() - t0) * 1000)
    if errors:
        # Concat the first 3 errors so the log row is bounded.
        result.error = " | ".join(errors[:3])
    return result
