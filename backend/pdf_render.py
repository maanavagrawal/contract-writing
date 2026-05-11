"""
Render a PDF to per-page PNGs with form-field metadata for edit-in-preview.

Used by /api/preview to give the frontend everything it needs to display the
PDF and let users edit fields directly:
  - PNG of each page (rendered without form fields, so we can overlay HTML)
  - List of fields per page with rects in HTML-image coordinates and their
    current /V values

Coordinate spaces involved (this is the part that bites you if you're not careful):
  PDF user space:  origin = bottom-left, units = points (1/72")
  PNG output:      origin = top-left,    units = pixels at SCALE * 72 dpi

We translate from PDF rects to PNG rects once, on the server, so the frontend
can position absolute-positioned HTML inputs directly in image-pixel space
without re-doing the y-flip.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import pypdfium2 as pdfium
from pypdf import PdfReader

from .pdf_introspect import walk_fields

# 2x renders crisp on retina without exploding payload size for typical
# 14-page contracts. Adjust if previews feel blurry or sluggish.
RENDER_SCALE = 2.0


@dataclass
class FieldOverlay:
    """One AcroForm field as the frontend needs to render an editable overlay."""
    name: str                       # dotted name, the same key fill_pdf takes
    field_type: str                 # "/Tx" | "/Btn" | "/Ch" | "/Sig"
    page: int                       # 1-based
    rect_px: tuple[float, float, float, float]  # (x, y, width, height) in PNG pixels
    value: str                      # current /V; "" if unset
    states: list[str]               # /Btn appearance state names (radio detection)


@dataclass
class PageRender:
    page: int                       # 1-based
    width_px: int
    height_px: int
    image_b64: str                  # PNG, base64-encoded


def _pdf_rect_to_image_px(
    rect: tuple[float, float, float, float],
    page_height_pt: float,
    scale: float,
) -> tuple[float, float, float, float]:
    """PDF rect (llx, lly, urx, ury) bottom-left → PNG rect (x, y, w, h) top-left."""
    llx, lly, urx, ury = rect
    x = llx * scale
    # Flip y: PDF lly is from bottom; image y is from top.
    y = (page_height_pt - ury) * scale
    w = (urx - llx) * scale
    h = (ury - lly) * scale
    return (x, y, w, h)


def _stringify_v(v) -> str:
    """/V can be a TextStringObject, NameObject, list, or None. Coerce to str.

    NameObjects keep their leading slash (e.g. "/On") because the frontend
    matches against the literal state name when round-tripping checkboxes
    through /api/edit + fill_pdf.
    """
    if v is None:
        return ""
    return str(v)


def render_pdf_for_edit(pdf_bytes: bytes) -> tuple[list[PageRender], list[FieldOverlay]]:
    """Returns (pages, fields). Pages are indexed 1..N matching field.page."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    fields = walk_fields(reader)

    # pypdfium2 owns its own copy of the bytes; load once.
    doc = pdfium.PdfDocument(pdf_bytes)
    page_heights_pt: dict[int, float] = {}

    pages: list[PageRender] = []
    try:
        for idx in range(len(doc)):
            page = doc[idx]
            width_pt, height_pt = page.get_size()
            page_heights_pt[idx + 1] = height_pt

            pil_image = page.render(scale=RENDER_SCALE).to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG", optimize=True)
            pages.append(PageRender(
                page=idx + 1,
                width_px=pil_image.width,
                height_px=pil_image.height,
                image_b64=base64.b64encode(buf.getvalue()).decode("ascii"),
            ))
            page.close()
    finally:
        doc.close()

    overlays: list[FieldOverlay] = []
    for fi in fields:
        v = _stringify_v(fi.leaf_obj.get("/V"))
        for w in fi.widgets:
            if w.page <= 0 or w.rect is None:
                continue
            page_h = page_heights_pt.get(w.page)
            if page_h is None:
                continue
            rect_px = _pdf_rect_to_image_px(w.rect, page_h, RENDER_SCALE)
            overlays.append(FieldOverlay(
                name=fi.dotted_name,
                field_type=fi.field_type,
                page=w.page,
                rect_px=rect_px,
                value=v,
                states=list(w.states),
            ))

    return pages, overlays


# ---------- Field crops for AI mapping enrichment ----------

# DPI scale for crops. Lower than RENDER_SCALE=2.0 because each crop becomes a
# vision API input and tokens scale with image size. 1.5x at our typical PDF
# page width gives a ~700-token crop that's legible to gpt-5.
CROP_SCALE = 1.5

# How much padding to include around the field rect, in PDF points.
# Labels typically live within 1-1.5 inches above and to the left of a field
# blank, so we render a band that captures those zones.
CROP_PADDING_TOP = 60      # ~0.83 inch — captures column headers
CROP_PADDING_LEFT = 180    # ~2.5 inches — captures inline labels
CROP_PADDING_RIGHT = 60    # smaller — checkbox labels usually right-adjacent
CROP_PADDING_BOTTOM = 12   # minimal — labels rarely live below the field


def _render_field_crop(
    doc: pdfium.PdfDocument,
    page_idx: int,
    rect: tuple[float, float, float, float],
) -> bytes:
    """Render a single field's neighborhood as a PNG.

    rect is the field's PDF-space rect (llx, lly, urx, ury) in PDF points.
    pypdfium2's `crop` parameter takes (left, bottom, right, top) amounts
    to cut off each edge in PDF canvas units — counter-intuitive but
    well-defined: the kept region is the page rect minus those cuts.

    PDF space has y growing upward, so the field's "above" zone (where
    labels live) is at higher y. We cut everything outside the band:
      keep_left   = field.llx - CROP_PADDING_LEFT
      keep_bottom = field.lly - CROP_PADDING_BOTTOM
      keep_right  = field.urx + CROP_PADDING_RIGHT
      keep_top    = field.ury + CROP_PADDING_TOP

    The cuts (what pypdfium2 wants) are page_size - keep_region.
    """
    page = doc[page_idx]
    try:
        page_w, page_h = page.get_size()
        llx, lly, urx, ury = rect

        # The "keep" window in PDF coords.
        keep_left = max(0.0, llx - CROP_PADDING_LEFT)
        keep_bottom = max(0.0, lly - CROP_PADDING_BOTTOM)
        keep_right = min(page_w, urx + CROP_PADDING_RIGHT)
        keep_top = min(page_h, ury + CROP_PADDING_TOP)

        # If the rect is malformed (zero or negative area after clamp),
        # render the whole page; better than a crash on weird widgets.
        if keep_right <= keep_left or keep_top <= keep_bottom:
            cut = (0.0, 0.0, 0.0, 0.0)
        else:
            cut_left = keep_left
            cut_bottom = keep_bottom
            cut_right = page_w - keep_right
            cut_top = page_h - keep_top
            cut = (cut_left, cut_bottom, cut_right, cut_top)

        pil_image = page.render(scale=CROP_SCALE, crop=cut).to_pil()
    finally:
        page.close()

    buf = io.BytesIO()
    pil_image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def collect_field_crops(
    reader: PdfReader,
    field_descriptions: list[dict],
) -> dict[str, str]:
    """For every field whose neighbor_text is empty, render a small PNG of
    the field's surrounding area and return it base64-encoded.

    Used by the AI mapping pipeline as visual context for fields where text
    extraction failed to capture a label. On Multi-Board 8.0, 22% of fields
    have empty neighbor_text — those are the fields that need this.

    Returns {pdf_field_name: base64_png_bytes}. Fields with usable text are
    NOT in the dict — saves API tokens by sending visual context only when
    text-only signal is missing.

    The reader argument is the same PdfReader the upload pipeline already
    has open. We re-open the bytes via pypdfium2 inside this function.
    """
    # Identify fields needing visual help: empty or near-empty neighbor_text.
    needs_crop: list[dict] = [
        fd for fd in field_descriptions
        if not (fd.get("neighbor_text") or "").strip()
    ]
    if not needs_crop:
        return {}

    # Build a name → primary widget rect/page lookup. The descriptors already
    # carry the page number; we need the rect too. Walk the PDF once to grab
    # the rects.
    #
    # _pick_primary_widget lives in templates.py (leading underscore = private)
    # but the heuristic is PDF-introspection logic that belongs alongside
    # walk_fields. TODO: move it to pdf_introspect.py. For now, accept the
    # cross-module private reference — function-local import avoids a circular
    # dep at module load (templates imports models, models has no deps from
    # us; safe both ways but explicit > implicit).
    from .templates import _pick_primary_widget
    rect_by_name: dict[str, tuple[int, tuple[float, float, float, float]]] = {}
    for fi in walk_fields(reader):
        primary = _pick_primary_widget(fi.widgets)
        if not primary or not primary.rect or primary.page <= 0:
            continue
        rect_by_name[fi.dotted_name] = (primary.page, primary.rect)

    # Stream the original PDF bytes through pypdfium2. The reader holds them
    # in its stream; rewind and read.
    reader.stream.seek(0)
    pdf_bytes = reader.stream.read()

    doc = pdfium.PdfDocument(pdf_bytes)
    crops: dict[str, str] = {}
    try:
        for fd in needs_crop:
            name = fd.get("pdf_field")
            if not name or name not in rect_by_name:
                continue
            page_num, rect = rect_by_name[name]
            try:
                png_bytes = _render_field_crop(doc, page_num - 1, rect)
            except Exception as e:
                # A single bad crop shouldn't blow up the upload. Log and skip.
                print(f"collect_field_crops: crop failed for {name!r}: {e}")
                continue
            crops[name] = base64.b64encode(png_bytes).decode("ascii")
    finally:
        doc.close()

    return crops
