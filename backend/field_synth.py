"""
Synthesize an AcroForm field tree for PDFs that have no `/AcroForm`.

The user uploads a flattened PDF (e.g. CAR Buyer Rep run through iLovePDF,
or any "saved as" PDF where the original AcroForm was destroyed). pypdf's
`walk_fields(reader)` returns empty for these. This module finds blank
underlines and checkboxes visually, then writes a real AcroForm widget
tree into the PDF so the rest of the upload pipeline (collect_field_
descriptions → propose_mapping → fill_pdf) works unchanged.

# Detection
For each page:
  1. Render to PNG at 200 DPI via pypdfium2 (existing dep).
  2. OpenCV morphological detection of long horizontal underlines.
  3. OpenCV findContours for small near-square outlines (checkboxes).
  4. Filter via pdfminer.six text-overlap — a rect with rendered text
     inside is a letter shape, not a blank. Kills 95% of false positives.
  5. Convert pixel coords back to PDF user-space (Y axis flips).

# Synthesis
Build a minimal pypdf AcroForm:
  /Root /AcroForm <<
    /Fields [...]
    /NeedAppearances true
  >>
Each synthetic field is a single-widget /Tx (text) or /Btn (checkbox)
with a unique /T name like "f_001_007" (page-3-indexed-from-zero, field-7
within that page). Names zero-pad to 3 digits so sort order matches
read order. /Rect comes from the converted pixel coords.

# Why CV+OpenCV rather than a paid service
At 2 paying users + 14-page contracts: $0/upload beats $2-4/upload via
Interfaze or Reducto for the field-detection stage. Spike showed ~90%
recall on the actual user PDF. If recall ever degrades, the Interfaze
shadow path (see scripts/interfaze_shadow.py) can be promoted to primary
with one env var.

# AcroForm-first short-circuit
This module is ONLY invoked when walk_fields(reader) returns empty. Users
uploading normal fillable PDFs (Multi-Board, Compass Lease Abstract) never
hit this code path.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np
import pypdfium2
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextBox, LTTextLine
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

# Render DPI for the CV pipeline. 200 DPI balances:
#   - underline detection: a 1pt underline is 2-3px at 200 DPI; below ~150
#     DPI the morphological filter starts dropping thin lines
#   - memory: 200 DPI on US Letter is ~1700×2200 px = ~14 MB per page bitmap
#   - latency: ~1-2s per page render via pypdfium2 on a single CPU
RENDER_DPI = 200
SCALE = RENDER_DPI / 72.0  # PDF user-space is 72 DPI


@dataclass(frozen=True)
class SynthField:
    """One synthesized field. page_idx is 0-based. rect is in PDF user-space
    (origin lower-left, Y bottom-up), matching the AcroForm /Rect convention.
    kind is 'text' (underline → /Tx) or 'checkbox' (small square → /Btn)."""
    page_idx: int
    rect: tuple[float, float, float, float]  # (llx, lly, urx, ury)
    kind: str  # 'text' or 'checkbox'

    @property
    def name(self) -> str:
        # Zero-padded so f_000_002 sorts before f_000_010. Lexical sort then
        # matches the natural reading order (page then index within page).
        # Future-proof to 999 pages × 999 fields per page.
        # The leading 'f_' marks these as synthetic — useful if downstream
        # code wants to render them differently in the review UI.
        # NOTE: index is filled in by the synthesizer, not here.
        raise NotImplementedError("use SynthField.with_index(i).name")


# ---- coordinate conversion ----

def _pixel_to_pdf_rect(
    px_rect: tuple[int, int, int, int],
    page_height_pt: float,
    scale: float = SCALE,
) -> tuple[float, float, float, float]:
    """Convert (x, y, w, h) from image pixels to PDF user-space (llx, lly, urx, ury).

    Image: origin top-left, Y top-down, pixels at `scale` factor of PDF points.
    PDF:   origin bottom-left, Y bottom-up, in points.

    The Y flip is the single most bug-prone transform in this module. Encoded
    once here so every caller goes through the same well-tested path.
    """
    x, y, w, h = px_rect
    llx = x / scale
    urx = (x + w) / scale
    # Top edge of the image rect (y) is the UPPER edge in PDF space.
    # Bottom edge of the image rect (y + h) is the LOWER edge in PDF space.
    # So in PDF coordinates: urly = page_height - y; lly = page_height - (y+h).
    ury = page_height_pt - (y / scale)
    lly = page_height_pt - ((y + h) / scale)
    return (llx, lly, urx, ury)


# ---- CV primitives ----

def _binarize(gray: np.ndarray) -> np.ndarray:
    """Invert + threshold: form features (lines, boxes) become white on black,
    which is what every OpenCV morphological op expects."""
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    return binary


def _detect_underlines_px(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Long horizontal lines = text-field underlines.

    Morphological open with a 50-pixel-wide kernel keeps only line-like
    structures. At 200 DPI, 50 px = 18 PDF points, which is the realistic
    minimum width of a real fillable underline. Tighter than 50 starts
    catching dashes and hyphens; looser misses initial-line fields."""
    binary = _binarize(gray)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (50, 1))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=1)
    contours, _ = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # Filters:
        #   width  >= 50 px (~18 PDF pt) — real fields
        #   height <= 12 px (~4 PDF pt)  — line, not a paragraph
        #   aspect ratio >= 8            — wide+thin, not a square
        if w < 50 or h > 12:
            continue
        if w / max(h, 1) < 8:
            continue
        # Synthesize a "field rect" sitting on top of the underline. The
        # rect's HEIGHT controls how tall the writeable area is — viewers
        # using /NeedAppearances draw the value inside this rect with the
        # font from /DA. Too short and the text overflows downward into
        # the row below (CAR BRBC 2026-05-12 bug: 5pt rect, 10pt font →
        # "Oakland" rendering halfway into the B(3) row).
        #
        # At 200 DPI: 32 px = ~11.5 PDF pt. That fits a ~6-8pt font with
        # padding above and below the underline.
        #
        # Clamp y >= 0 so an underline near the top of the image doesn't
        # spawn a widget with negative pixel-y (which would become an
        # out-of-page PDF rect post Y-flip).
        field_h = max(h * 7, 32)
        new_y = max(0, y - field_h + h)
        out.append((x, new_y, w, field_h))
    return out


def _detect_checkboxes_px(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Small near-square outlines.

    Constraints (all at 200 DPI):
      - 20-70 px square (≈7-25 PDF pt — real checkbox size range)
      - aspect ratio ≤ 1.3 (near-square)
      - contour approximates to 4 vertices (rectangle, not letter curve)
      - contour area ≥ 100 px² (drops noise)

    The text-overlap filter (next stage) kills surviving false positives
    where rounded capital letters (D, O, Q, etc.) sneak through the
    geometric filters."""
    binary = _binarize(gray)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (20 <= w <= 70 and 20 <= h <= 70):
            continue
        if max(w, h) / max(min(w, h), 1) > 1.3:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.04 * peri, True)
        if len(approx) != 4:
            continue
        area = cv2.contourArea(c)
        if area < 100:
            continue
        out.append((x, y, w, h))
    return out


def _dedupe_rects(
    rects: list[tuple[int, int, int, int]],
    tol: int = 5,
) -> list[tuple[int, int, int, int]]:
    """Near-duplicate dedupe — same upper-left + same size within `tol` px.
    O(n²) is fine for n<200 (typical page has <80 candidates)."""
    seen: list[tuple[int, int, int, int]] = []
    for r in rects:
        if any(
            abs(r[0] - s[0]) < tol and abs(r[1] - s[1]) < tol
            and abs(r[2] - s[2]) < tol and abs(r[3] - s[3]) < tol
            for s in seen
        ):
            continue
        seen.append(r)
    return seen


# ---- text-overlap filter (kills letter-shape FPs) ----

def _text_boxes_all_pages(
    pdf_bytes: bytes,
) -> dict[int, list[tuple[float, float, float, float]]]:
    """Extract every text-line bounding box on every page in ONE pdfminer
    parse. Returns {page_idx: [bbox, ...]}.

    pdfminer is the most expensive part of the filter — without this batch,
    a 14-page contract reparsed 14x = minutes of CPU on Railway. One full
    extract_pages() pass is ~3-5s total.

    Returns an empty dict if pdfminer fails; the filter then no-ops and
    every CV candidate survives — graceful degradation over hard failure."""
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    try:
        for page_idx, page_layout in enumerate(extract_pages(io.BytesIO(pdf_bytes))):
            boxes: list[tuple[float, float, float, float]] = []
            for element in page_layout:
                # LTTextBox aggregates lines; we keep both granularities so
                # short labels (Date, By) and multi-word labels both
                # participate in the overlap test.
                if isinstance(element, (LTTextBox, LTTextLine)):
                    boxes.append(element.bbox)
                    if isinstance(element, LTTextBox):
                        for line in element:
                            if isinstance(line, LTTextLine):
                                boxes.append(line.bbox)
            out[page_idx] = boxes
    except Exception:
        # pdfminer is finicky on unusual PDFs — better to skip the overlap
        # filter than to fail the upload. The AI mapping step downstream
        # gives every field a confidence score and the user reviews
        # low-confidence ones anyway.
        return {}
    return out


def _rect_overlaps_text(
    rect: tuple[float, float, float, float],
    text_boxes: list[tuple[float, float, float, float]],
    min_overlap_ratio: float = 0.6,
) -> bool:
    """True if `rect` is mostly contained within any text bounding box.

    A "checkbox" that overlaps 60%+ with rendered text is a letter shape, not
    a real checkbox. We use containment (rect-area ∩ text-area) / rect-area
    rather than IoU — a small letter inside a giant text paragraph should
    still trigger.
    """
    llx, lly, urx, ury = rect
    rect_area = max((urx - llx) * (ury - lly), 1.0)
    for tx0, ty0, tx1, ty1 in text_boxes:
        ix0 = max(llx, tx0)
        iy0 = max(lly, ty0)
        ix1 = min(urx, tx1)
        iy1 = min(ury, ty1)
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        inter = (ix1 - ix0) * (iy1 - iy0)
        if inter / rect_area >= min_overlap_ratio:
            return True
    return False


# ---- public detection entry ----

def detect_blanks(pdf_bytes: bytes) -> list[SynthField]:
    """Find every blank text field and checkbox in a flattened PDF.

    Returns one SynthField per detected blank. Memory bound: one page's
    bitmap + numpy gray buffer at any time (~14 MB at 200 DPI). Bitmaps
    are explicitly closed so pypdfium2's C-side memory is released
    immediately rather than waiting on Python GC.

    pdfminer text extraction runs ONCE across all pages (not per-page) —
    that pass is ~3-5s total vs. 14× per-page = minutes on a 14-page CAR
    contract.
    """
    # One full-document pdfminer pass up front. Cheap when filter is no-op
    # (genuine scan → empty dict → every candidate survives, AI mapping
    # gives confidence=0).
    text_boxes_by_page = _text_boxes_all_pages(pdf_bytes)

    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        out: list[SynthField] = []
        for page_idx in range(len(pdf)):
            page = pdf[page_idx]
            bitmap = None
            try:
                page_height_pt = float(page.get_height())
                bitmap = page.render(scale=SCALE, rotation=0)
                pil = bitmap.to_pil()
                bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            finally:
                # Order matters: close bitmap before page. pypdfium2 holds
                # C-side memory in the bitmap that GC may not reclaim
                # promptly enough on a 30-page contract → OOM on Railway.
                if bitmap is not None:
                    try:
                        bitmap.close()
                    except Exception:
                        pass
                page.close()

            underlines = _dedupe_rects(_detect_underlines_px(gray))
            checkboxes = _dedupe_rects(_detect_checkboxes_px(gray))

            # Text-overlap filter — only run if we got any candidates AND
            # any text on the page. Empty page (a divider) gets no fields,
            # which is correct.
            if underlines or checkboxes:
                text_boxes = text_boxes_by_page.get(page_idx, [])

                # Underlines need a gentler overlap threshold — they sit
                # under (not on top of) text labels, so partial overlap is
                # normal and should be kept.
                kept_underlines = [
                    r for r in underlines
                    if not _rect_overlaps_text(
                        _pixel_to_pdf_rect(r, page_height_pt),
                        text_boxes,
                        min_overlap_ratio=0.85,
                    )
                ]
                kept_checkboxes = [
                    r for r in checkboxes
                    if not _rect_overlaps_text(
                        _pixel_to_pdf_rect(r, page_height_pt),
                        text_boxes,
                        min_overlap_ratio=0.5,
                    )
                ]
            else:
                kept_underlines = underlines
                kept_checkboxes = checkboxes

            for px in kept_underlines:
                out.append(SynthField(
                    page_idx=page_idx,
                    rect=_pixel_to_pdf_rect(px, page_height_pt),
                    kind="text",
                ))
            for px in kept_checkboxes:
                out.append(SynthField(
                    page_idx=page_idx,
                    rect=_pixel_to_pdf_rect(px, page_height_pt),
                    kind="checkbox",
                ))
        return out
    finally:
        pdf.close()


# ---- AcroForm synthesis ----

def _make_widget_dict(
    field_name: str,
    rect: tuple[float, float, float, float],
    kind: str,
) -> DictionaryObject:
    """Build one merged field+widget annotation dictionary.

    For single-widget /Tx and /Btn fields, the spec allows the field
    dictionary and widget annotation to be the same object — `/FT` (field
    type) and `/Subtype /Widget` (annotation type) live side-by-side. This
    is also the shape pypdf's fill_pdf code already understands.
    """
    llx, lly, urx, ury = rect
    d = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/T"): TextStringObject(field_name),
        NameObject("/Rect"): ArrayObject([
            FloatObject(llx), FloatObject(lly),
            FloatObject(urx), FloatObject(ury),
        ]),
        # /F = 4 (Print flag on, Hidden off). Without it some viewers
        # don't render the widget at all.
        NameObject("/F"): NumberObject(4),
    })
    if kind == "checkbox":
        d[NameObject("/FT")] = NameObject("/Btn")
        # Single-state checkbox: /V default "Off", value flipped to "Yes"
        # by fill_pdf when checked. Field flags 0 (no Radio, no PushButton,
        # no NoToggleToOff — vanilla checkbox).
        d[NameObject("/Ff")] = NumberObject(0)
        d[NameObject("/V")] = NameObject("/Off")
        d[NameObject("/AS")] = NameObject("/Off")
        # /AP appearance streams are NOT generated here. Two pragmatic reasons:
        # (1) Generating proper /AP/N + /AP/D dicts means building Form XObject
        # streams with content commands (BT/ET text or path operators) — ~80
        # LOC, lots of edge cases, and pypdf doesn't have a one-call helper.
        # (2) Setting /NeedAppearances=true at the AcroForm level (which
        # synthesize_acroform does) tells viewers to render widgets from
        # /V — including /Btn checkboxes. Acrobat, Chrome, Firefox honor
        # this. Apple Preview historically did NOT for /Btn until ~Sonoma,
        # so users on macOS 13 or earlier may see filled checkboxes render
        # blank. Acceptable trade for the user base (mostly Chrome/Safari
        # current). Revisit if a design partner reports missing checkmarks.
    else:
        d[NameObject("/FT")] = NameObject("/Tx")
        d[NameObject("/Ff")] = NumberObject(0)
        d[NameObject("/V")] = TextStringObject("")
        # /DA = default appearance. Font size MUST fit the rect height or
        # /NeedAppearances viewers render text spilling out of the widget —
        # the CAR BRBC 2026-05-12 bug ("Oakland" rendering halfway into the
        # B(3) row) traced back to a fixed 10pt font in a 5pt-tall rect.
        #
        # Underline-derived rects span the underline + ~3-line-heights above
        # it. The visual "writeable area" the user sees on the printed form
        # is roughly the rect height in PDF points. Pick a font size that
        # leaves ~25% top/bottom padding so descenders don't clip into the
        # underline and ascenders don't clip into the label above.
        rect_h_pt = max(0.0, ury - lly)
        # 0.55 multiplier: 10pt rect → 5.5pt text. 14pt rect → 7.7pt. 20pt
        # rect → 11pt. Clamp to [6, 11]: below 6pt is unreadable; above 11pt
        # starts looking oversized vs the surrounding form text.
        font_pt = max(6.0, min(11.0, rect_h_pt * 0.55))
        d[NameObject("/DA")] = TextStringObject(f"/Helv {font_pt:.1f} Tf 0 g")
        # /Q = quadding (text alignment). 0=left, 1=center, 2=right. Left
        # matches every form blank in the wild.
        d[NameObject("/Q")] = NumberObject(0)
    return d


def synthesize_acroform(pdf_bytes: bytes, fields: list[SynthField]) -> bytes:
    """Write a real `/AcroForm` into the PDF so walk_fields can see the
    synthesized fields. Returns new PDF bytes.

    The synthesized PDF round-trips: feed it into PdfReader → walk_fields
    finds every SynthField as a proper FieldInfo, fill_pdf treats them
    identically to AcroForm-original widgets, and the existing AI mapping
    pipeline sees them with neighbor-text extracted by extract_neighbor_text.

    Per-page widgets are attached to that page's /Annots array. The top-level
    /AcroForm/Fields array references every widget dict by indirect ref.
    """
    if not fields:
        return pdf_bytes

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=reader)

    # Group by page so we can extend each page's /Annots in one pass.
    fields_by_page: dict[int, list[SynthField]] = {}
    for fld in fields:
        fields_by_page.setdefault(fld.page_idx, []).append(fld)

    all_field_refs: list[DictionaryObject] = []

    for page_idx, page_fields in sorted(fields_by_page.items()):
        if page_idx >= len(writer.pages):
            continue
        page = writer.pages[page_idx]

        # /Annots may not exist on a flattened page — create as empty array.
        if "/Annots" not in page:
            page[NameObject("/Annots")] = ArrayObject([])
        annots = page["/Annots"]
        if hasattr(annots, "get_object"):
            annots = annots.get_object()

        for idx, fld in enumerate(page_fields):
            # Zero-padded synthetic /T. Page index 3 digits, within-page 3
            # digits. Lexical sort matches reading order.
            field_name = f"f_{page_idx:03d}_{idx:03d}"
            widget = _make_widget_dict(field_name, fld.rect, fld.kind)
            # Register as an indirect object so both /AcroForm/Fields and
            # the page /Annots can reference the same dict.
            widget_ref = writer._add_object(widget)
            annots.append(widget_ref)
            all_field_refs.append(widget_ref)

    # Build /AcroForm. /NeedAppearances=true tells viewers to render widget
    # values from their /V (since we don't ship /AP appearance streams).
    catalog = writer._root_object
    acroform = DictionaryObject({
        NameObject("/Fields"): ArrayObject(all_field_refs),
        NameObject("/NeedAppearances"): BooleanObject(True),
    })
    catalog[NameObject("/AcroForm")] = acroform

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---- combined entry for templates.validate_pdf ----

def try_synthesize(pdf_bytes: bytes) -> tuple[bytes, int]:
    """Run detection + synthesis. Returns (new_pdf_bytes, n_fields_added).

    Convenience entry for `templates.validate_pdf`. If detection returns 0
    fields (genuine scan or content-only PDF), returns the original bytes
    and n=0 — caller decides whether to fail gracefully or fall through.

    Never raises. Network/CV exceptions get logged via the standard backend
    log; the upload caller treats "0 fields synthesized" the same as a
    pre-CV-era flattened-PDF rejection (graceful error, user sees a clear
    message instead of a 500).
    """
    try:
        fields = detect_blanks(pdf_bytes)
    except Exception as e:
        print(f"field_synth: detect_blanks failed: {type(e).__name__}: {e}", flush=True)
        return (pdf_bytes, 0)
    if not fields:
        return (pdf_bytes, 0)
    try:
        new_bytes = synthesize_acroform(pdf_bytes, fields)
    except Exception as e:
        print(f"field_synth: synthesize_acroform failed: {type(e).__name__}: {e}", flush=True)
        return (pdf_bytes, 0)
    return (new_bytes, len(fields))
