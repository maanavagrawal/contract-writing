"""
Signature image stamping for AcroForm signature fields.

The flow:
  1. Caller hands us a parsed `PdfReader`, a list of `(page_idx, rect, png_bytes)`
     stamps, and gets back a watermarked overlay PDF as bytes.
  2. We render the stamps onto a fresh PDF via reportlab (which handles
     RGBA + transparency cleanly via /SMask soft-masks — pypdf's bare-metal
     image embedding does not).
  3. The caller merges that overlay onto the source PDF via
     `pypdf.PageObject.merge_page()`. merge_page handles /Rotate-rotated
     pages, /CropBox clipping, and content-stream isolation (`q ... Q`)
     correctly, so we get a single primitive that works on every CAR /
     Multi-Board / Compass template without per-template special-casing.

Why reportlab (and not hand-rolled XObject embedding):
  - PNG with alpha → /DeviceRGB + /SMask is ~80 lines of pypdf bookkeeping
    where every line is a footgun (forgetting /SMask = opaque white box
    behind the stroke; wrong /BitsPerComponent = render artifacts).
  - reportlab.canvas.drawImage handles the RGB/alpha split internally.
  - Cost: one extra runtime dep (~3MB). Acceptable for the safety.

Why page-by-page overlay (instead of one mega-overlay PDF):
  - The source PDF may have heterogeneous page sizes (legal + letter on the
    same document — Multi-Board does this on the addendum). Reportlab
    needs the actual pagesize per page; iterating per page is the same
    cost as iterating per stamp and cleaner to reason about.
  - merge_page mutates one page at a time. Simpler error recovery: a single
    bad stamp doesn't poison the rest of the document.

Page rotation handling:
  - AcroForm /Rect is in PDF user-space (pre-rotation).
  - reportlab draws in user-space too.
  - merge_page composites the overlay in the SAME user-space, so as long as
    the source page's /Rotate matches the overlay's (we set it to 0 — no
    rotation needed on the overlay since the source already declares its
    rotation), the visible result is correct.
  - The bug to NOT introduce: counter-rotating the rect math ourselves AND
    relying on merge_page's rotation handling — that double-rotates. Don't.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Literal

from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


# A signature placement: which page (1-based), where on that page (PDF
# user-space rect), and what PNG to stamp. The caller resolves these from
# the AcroForm + agent_defaults pair; this module doesn't touch storage.
@dataclass(frozen=True)
class StampPlacement:
    page: int                                # 1-based page number
    rect: tuple[float, float, float, float]  # (llx, lly, urx, ury) in PDF points
    png_bytes: bytes                         # decoded PNG bytes; we hand them to reportlab as-is


# How much to let the signature bleed past the rect horizontally (per the
# design-review finding: real signatures overshoot the line slightly; strict
# containment reads as "stamped by a bureaucrat"). 10% of the rect width is
# the sweet spot — visible bleed without looking sloppy. Clipped to page
# bounds naturally by the PDF renderer.
HORIZONTAL_BLEED_FRACTION = 0.10


class InvalidSignaturePng(ValueError):
    """Raised when the supplied PNG can't be loaded by reportlab. Callers
    catch this at the generate.py layer and surface as a soft warning in
    the response instead of 500-ing on a corrupt blob."""


def _fit_image_in_rect(
    png_bytes: bytes,
    rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Compute the (x, y, w, h) at which to draw the image inside `rect`.

    Behavior:
      - Preserve aspect ratio.
      - Vertical-center inside the rect (signatures usually sit a hair
        above the baseline; bottom-anchor leaves them floating, top-anchor
        crushes them into the field label above).
      - Left-align horizontally (matches how someone writes left-to-right
        on a paper signature line).
      - Allow up to HORIZONTAL_BLEED_FRACTION of the rect width to spill
        past the right edge — real signatures overshoot.

    Returns (x_bottom_left, y_bottom_left, width, height) ready for
    reportlab's drawImage."""
    llx, lly, urx, ury = rect
    rect_w = urx - llx
    rect_h = ury - lly
    if rect_w <= 0 or rect_h <= 0:
        # Degenerate rect — skip without raising. Field probably came from
        # a malformed widget /Rect; drawing a zero-size image is a no-op
        # that won't poison the rest of the stamping run.
        return (llx, lly, 0.0, 0.0)

    # Reportlab needs the natural image size for aspect math.
    try:
        img = ImageReader(io.BytesIO(png_bytes))
        img_w, img_h = img.getSize()
    except Exception as e:
        raise InvalidSignaturePng(f"could not decode signature PNG: {e}") from e

    if img_w <= 0 or img_h <= 0:
        raise InvalidSignaturePng(f"signature PNG has invalid size: {img_w}x{img_h}")

    # Permitted draw box: full rect height, rect width plus 10% bleed.
    permitted_w = rect_w * (1.0 + HORIZONTAL_BLEED_FRACTION)
    permitted_h = rect_h

    # Scale to fit (preserve aspect; pick the smaller scale so neither
    # dimension overflows).
    scale_w = permitted_w / img_w
    scale_h = permitted_h / img_h
    scale = min(scale_w, scale_h)

    draw_w = img_w * scale
    draw_h = img_h * scale

    # Left-align horizontally; vertical-center.
    x = llx
    y = lly + (rect_h - draw_h) / 2.0

    return (x, y, draw_w, draw_h)


def build_stamp_overlay(
    reader: PdfReader,
    placements: list[StampPlacement],
) -> bytes:
    """Render a transparent overlay PDF the same shape as `reader`, with
    each placement's PNG drawn at its rect on its page. Returns the overlay
    PDF as bytes; caller merges it onto the source.

    Pages without any placement get an empty page of the matching size so
    the page count + dimensions line up for merge_page.

    Raises InvalidSignaturePng if any PNG fails to decode — caller catches
    and proceeds with a blank field instead of crashing the whole fill."""
    if not placements:
        # No work to do. Return empty bytes; caller checks and skips merge.
        return b""

    by_page: dict[int, list[StampPlacement]] = {}
    for p in placements:
        by_page.setdefault(p.page, []).append(p)

    buf = io.BytesIO()
    # reportlab's Canvas pagesize is set per-page via setPageSize() below.
    c = canvas.Canvas(buf)

    for page_idx, source_page in enumerate(reader.pages, start=1):
        # Match the source page's MediaBox dimensions so the overlay merges
        # cleanly. Width/height come off the page object's /MediaBox (or
        # parent-inherited box). Reportlab wants (w, h) in points.
        try:
            mb = source_page.mediabox
            page_w = float(mb.width)
            page_h = float(mb.height)
        except Exception:
            # Fall back to US Letter (612x792). Pages with malformed
            # MediaBoxes are rare enough that mis-sized overlay is OK —
            # merge_page will clip the excess.
            page_w, page_h = 612.0, 792.0

        c.setPageSize((page_w, page_h))

        page_placements = by_page.get(page_idx, [])
        for placement in page_placements:
            x, y, w, h = _fit_image_in_rect(placement.png_bytes, placement.rect)
            if w == 0 or h == 0:
                continue
            try:
                img = ImageReader(io.BytesIO(placement.png_bytes))
                # mask='auto' tells reportlab to use the PNG's alpha
                # channel as a soft mask. Without this, transparent
                # backgrounds render opaque white — the #1 signature-stamp
                # bug across PDF tooling.
                c.drawImage(img, x, y, width=w, height=h, mask="auto",
                            preserveAspectRatio=True, anchor="sw")
            except Exception as e:
                raise InvalidSignaturePng(
                    f"could not draw signature on page {page_idx}: {e}"
                ) from e

        c.showPage()

    c.save()
    return buf.getvalue()


def apply_stamps(
    reader: PdfReader,
    placements: list[StampPlacement],
) -> bytes:
    """Take a parsed source PDF + signature placements, return the source
    bytes with every PNG composited at its rect. The source's existing
    content (text, form fields, prior annotations) is preserved — the
    overlay merges on TOP of it.

    Empty `placements` list → returns the source unchanged (no overlay
    construction cost). Same for any page with no placement.

    This is the only function callers outside this module should use.
    `build_stamp_overlay` is exposed for testing."""
    if not placements:
        # Caller asked for nothing; give them the source back. Round-trip
        # through PdfWriter so the caller always gets a clean cloned copy
        # (matches fill_pdf's contract).
        writer = PdfWriter(clone_from=reader)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    overlay_bytes = build_stamp_overlay(reader, placements)
    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))

    writer = PdfWriter(clone_from=reader)
    # Merge per-page. merge_page composites the overlay's content stream
    # under a `q ... Q` (graphics-state isolation) wrapper, so the stamps
    # can't corrupt the source page's state machine.
    for page_idx, dest_page in enumerate(writer.pages):
        if page_idx >= len(overlay_reader.pages):
            # Source has more pages than overlay (overlay should match,
            # but defend in case of /Pages tree weirdness).
            break
        overlay_page = overlay_reader.pages[page_idx]
        dest_page.merge_page(overlay_page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---- Sentinel type for the rendered dict ---------------------------------

# Used by generate.py → pdf_fill to signal "this field gets a signature
# stamp, not a /V text write." A dataclass (not a string sentinel) is type-
# safe and can't collide with a user-supplied value. pdf_fill branches on
# isinstance(value, SigStamp).
@dataclass(frozen=True)
class SigStamp:
    kind: Literal["agent", "initials"]
    png_bytes: bytes
