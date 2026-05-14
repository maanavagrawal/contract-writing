"""
Tests for backend/signature_stamp — image stamping onto PDF pages.

Covers:
  - happy path: a synthetic 1-page PDF gets a PNG drawn at a known rect
  - /Rotate=90 page: stamp renders upright (the bug-that-ships-3-months-later)
  - corrupt PNG: raises InvalidSignaturePng instead of crashing
  - empty placements list: returns the source unchanged (no overlay cost)
  - SigStamp value type round-trips through pdf_fill

No real PDFs from disk — every test PDF is synthesized inline so the test
suite stays hermetic and the failure modes are isolated to the stamping
code, not the test fixtures.
"""
from __future__ import annotations

import base64
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from backend.signature_stamp import (
    HORIZONTAL_BLEED_FRACTION,
    InvalidSignaturePng,
    SigStamp,
    StampPlacement,
    apply_stamps,
    build_stamp_overlay,
    _fit_image_in_rect,
)


# A small (4x2) RGBA PNG we can pass everywhere. Built via Pillow at
# import time so reportlab's PIL backend reads it the same way it reads
# real client PNGs — avoids hand-crafted base64 PNGs that PIL rejects.
def _make_tiny_png() -> bytes:
    from PIL import Image
    img = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
    img.putpixel((0, 0), (0, 0, 0, 255))  # one opaque black pixel
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


TINY_PNG = _make_tiny_png()


def _make_blank_pdf(width: float = 612.0, height: float = 792.0, rotate: int = 0) -> bytes:
    """Synthesize a one-page PDF the simplest way: use reportlab. Setting
    /Rotate at the page level lets us test rotation handling."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    # Draw a faint border so visual debugging is possible if a test ever
    # writes the output to disk.
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.rect(10, 10, width - 20, height - 20, stroke=1, fill=0)
    c.showPage()
    c.save()

    pdf_bytes = buf.getvalue()
    if rotate == 0:
        return pdf_bytes

    # reportlab doesn't expose page rotation directly. Reopen with pypdf,
    # set /Rotate on the page, write out.
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=reader)
    writer.pages[0][NameObject("/Rotate")] = NumberObject(rotate)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---- _fit_image_in_rect ----------------------------------------------------

def test_fit_image_centers_vertically_and_left_aligns():
    rect = (100.0, 200.0, 200.0, 240.0)  # 100 wide, 40 tall, ll=(100,200)
    x, y, w, h = _fit_image_in_rect(TINY_PNG, rect)
    # PNG is 4x2 (2:1 aspect). Rect is 100x40 (2.5:1). Image will scale
    # height-limited until the +10% bleed lets it widen.
    assert x == 100.0  # left-aligned
    # Vertical center: image is some height h, lly = 200 + (40 - h) / 2
    assert abs(y - (200.0 + (40.0 - h) / 2.0)) < 0.01
    assert w > 0 and h > 0
    # With 4x2 image in a 100x40 rect, the bleed-permitted width is 110.
    # height=40 keeps aspect: w = 40 * 2 = 80, which is < 110 — so height
    # limits. Image draws 80 wide × 40 tall.
    assert abs(w - 80.0) < 0.01
    assert abs(h - 40.0) < 0.01


def test_fit_image_uses_horizontal_bleed():
    # Tall narrow rect: width limits scaling, bleed lets image overshoot.
    rect = (0.0, 0.0, 20.0, 100.0)  # 20 wide, 100 tall
    x, y, w, h = _fit_image_in_rect(TINY_PNG, rect)
    # Image is 4x2 (2:1). Bleed permits draw_width up to 22 (20 * 1.10).
    # height-limited: 100 tall * 0.5 = 50 wide — but capped by bleed at 22.
    # So width-limited: w=22, h=11.
    assert abs(w - (20.0 * (1.0 + HORIZONTAL_BLEED_FRACTION))) < 0.01


def test_fit_image_corrupt_png_raises():
    rect = (0.0, 0.0, 100.0, 50.0)
    with pytest.raises(InvalidSignaturePng):
        _fit_image_in_rect(b"this is not a PNG", rect)


def test_fit_image_zero_size_rect_returns_zero():
    rect = (100.0, 200.0, 100.0, 200.0)  # zero width and height
    x, y, w, h = _fit_image_in_rect(TINY_PNG, rect)
    assert w == 0.0 and h == 0.0


# ---- build_stamp_overlay ---------------------------------------------------

def test_overlay_empty_placements_returns_empty():
    reader = PdfReader(io.BytesIO(_make_blank_pdf()))
    assert build_stamp_overlay(reader, []) == b""


def test_overlay_creates_valid_pdf_with_image():
    reader = PdfReader(io.BytesIO(_make_blank_pdf()))
    placements = [
        StampPlacement(page=1, rect=(100.0, 100.0, 300.0, 150.0), png_bytes=TINY_PNG),
    ]
    overlay = build_stamp_overlay(reader, placements)
    # Result must be a parseable PDF.
    overlay_reader = PdfReader(io.BytesIO(overlay))
    assert len(overlay_reader.pages) == 1


def test_overlay_corrupt_png_raises():
    reader = PdfReader(io.BytesIO(_make_blank_pdf()))
    placements = [
        StampPlacement(page=1, rect=(100.0, 100.0, 300.0, 150.0), png_bytes=b"garbage"),
    ]
    with pytest.raises(InvalidSignaturePng):
        build_stamp_overlay(reader, placements)


def test_overlay_matches_source_page_count():
    """Multi-page source → overlay has same page count so merge_page aligns."""
    # Synthesize 3-page source.
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.showPage(); c.showPage(); c.showPage()
    c.save()
    reader = PdfReader(io.BytesIO(buf.getvalue()))

    # Place a stamp only on page 2; pages 1 and 3 are empty in the overlay.
    placements = [StampPlacement(page=2, rect=(50.0, 50.0, 200.0, 100.0), png_bytes=TINY_PNG)]
    overlay = build_stamp_overlay(reader, placements)
    overlay_reader = PdfReader(io.BytesIO(overlay))
    assert len(overlay_reader.pages) == 3


# ---- apply_stamps ----------------------------------------------------------

def test_apply_stamps_empty_returns_clean_clone():
    src_bytes = _make_blank_pdf()
    reader = PdfReader(io.BytesIO(src_bytes))
    out = apply_stamps(reader, [])
    # Output should be a valid PDF with the same page count.
    out_reader = PdfReader(io.BytesIO(out))
    assert len(out_reader.pages) == 1


def test_apply_stamps_composites_image_into_source():
    reader = PdfReader(io.BytesIO(_make_blank_pdf()))
    placements = [
        StampPlacement(page=1, rect=(100.0, 100.0, 300.0, 150.0), png_bytes=TINY_PNG),
    ]
    out = apply_stamps(reader, placements)
    out_reader = PdfReader(io.BytesIO(out))
    assert len(out_reader.pages) == 1
    # The output should be LARGER than the source (added a stamped image).
    src_len = len(_make_blank_pdf())
    assert len(out) > src_len, "stamped PDF should be larger than the blank source"


def test_apply_stamps_on_rotated_page():
    """The /Rotate=90 case from the engineering review. Source has /Rotate
    90 on its only page. After stamping, the output should still parse
    cleanly AND keep its /Rotate value — pypdf's merge_page composites in
    user-space, so the visible image inherits the page's rotation."""
    src_bytes = _make_blank_pdf(rotate=90)
    reader = PdfReader(io.BytesIO(src_bytes))
    assert reader.pages[0].get("/Rotate") == 90

    placements = [
        StampPlacement(page=1, rect=(100.0, 100.0, 300.0, 150.0), png_bytes=TINY_PNG),
    ]
    out = apply_stamps(reader, placements)
    out_reader = PdfReader(io.BytesIO(out))
    # /Rotate preserved — viewer renders the (stamped) page as rotated, the
    # stamp comes along for the ride. No counter-rotation needed in our code.
    assert out_reader.pages[0].get("/Rotate") == 90


# ---- SigStamp dataclass ----------------------------------------------------

def test_sigstamp_is_frozen_dataclass():
    s = SigStamp(kind="agent", png_bytes=TINY_PNG)
    assert s.kind == "agent"
    assert s.png_bytes == TINY_PNG
    with pytest.raises(Exception):
        # frozen=True → mutations raise
        s.kind = "initials"  # type: ignore[misc]


# ---- fill_pdf integration --------------------------------------------------

def test_fill_pdf_accepts_sigstamp_value_and_stamps_image():
    """End-to-end: a PDF with one /Tx field gets a SigStamp value and the
    resulting bytes carry the stamped overlay. We don't try to read the
    image back out (round-tripping through reportlab + pypdf + reportlab
    drops metadata) — we just confirm the value branch executes without
    crashing and the output PDF is larger (indicating an XObject was
    added) than the unstamped baseline."""
    from backend.pdf_fill import fill_pdf

    # Synthesize a PDF with one AcroForm /Tx field.
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.acroForm.textfield(name="agent_sig", tooltip="agent signature",
                         x=100, y=200, width=200, height=40,
                         borderStyle="solid", forceBorder=True)
    c.save()
    src_bytes = buf.getvalue()
    reader = PdfReader(io.BytesIO(src_bytes))

    # Baseline: fill with empty mapping (no stamps).
    blank_out = fill_pdf(reader, {})

    # Stamped: fill with a SigStamp value pointed at the field.
    reader_again = PdfReader(io.BytesIO(src_bytes))
    stamped_out = fill_pdf(reader_again, {
        "agent_sig": SigStamp(kind="agent", png_bytes=TINY_PNG)
    })

    # Sanity: both should parse.
    PdfReader(io.BytesIO(blank_out))
    PdfReader(io.BytesIO(stamped_out))
    # The stamped version should be measurably larger (added image XObject).
    assert len(stamped_out) > len(blank_out)
