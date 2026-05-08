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
