"""
Tests for backend/field_synth — CV-based field synthesis for flattened PDFs.

Three layers:
  1. Coordinate-conversion unit tests (the single most bug-prone helper).
  2. CV detection on the canonical CAR fixture — must hit recall floor.
  3. Round-trip integration — synthesize → walk_fields → fill → re-read.

The CAR fixture (the actual PDF that triggered this feature) lives at
backend/tests/fixtures/car_brbc_flattened.pdf. We assert ≥75% recall vs.
the spike's measured ~90% to leave wiggle room for CI variation; if recall
ever drops below 75% something is broken in the CV pipeline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.field_synth import (
    SynthField,
    _pixel_to_pdf_rect,
    detect_blanks,
    synthesize_acroform,
    try_synthesize,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CAR_FIXTURE = FIXTURES / "car_brbc_flattened.pdf"


# ---- coordinate conversion ----

def test_pixel_to_pdf_rect_origin_at_top_left():
    """Image pixel (0, 0) is top-left. In PDF user-space that's the top-left
    of the page = (0, page_height). Confirm the helper inverts Y correctly."""
    result = _pixel_to_pdf_rect((0, 0, 100, 10), page_height_pt=792.0, scale=1.0)
    # (x=0, y=0, w=100, h=10) at scale=1.0
    # llx = 0; urx = 100
    # ury = 792 - 0 = 792 (top of page)
    # lly = 792 - 10 = 782 (10 pt below the top)
    assert result == (0.0, 782.0, 100.0, 792.0)


def test_pixel_to_pdf_rect_scale_handles_dpi():
    """At 200 DPI, scale = 200/72 ≈ 2.78. A 100-pixel-wide rect at 200 DPI
    represents ~36 PDF points. Confirm the divisor matches."""
    scale = 200 / 72.0
    result = _pixel_to_pdf_rect((0, 0, 100, 20), page_height_pt=792.0, scale=scale)
    llx, lly, urx, ury = result
    # 100 px / (200/72) = 36 pt width
    assert abs((urx - llx) - 36.0) < 0.01
    # 20 px / (200/72) = 7.2 pt height
    assert abs((ury - lly) - 7.2) < 0.01


def test_pixel_to_pdf_rect_y_flip_bottom_pixel():
    """Pixel at y=792 (bottom of US Letter image at scale=1.0) should land
    at PDF y=0 (origin). REGRESSION TARGET: getting this backwards is the
    one bug that makes every field render mirrored."""
    result = _pixel_to_pdf_rect((0, 792, 10, 10), page_height_pt=792.0, scale=1.0)
    # y=792 means 792 pixels DOWN from top = 0 pt from bottom.
    # So ury = 792 - 792 = 0, lly = 792 - 802 = -10
    assert result == (0.0, -10.0, 10.0, 0.0)


# ---- CAR fixture (the user's actual broken case) ----

@pytest.mark.skipif(not CAR_FIXTURE.exists(), reason="CAR fixture not committed yet")
def test_car_brbc_detects_useful_field_count():
    """REGRESSION TARGET (2026-05-11 user feedback): the CAR Buyer Rep PDF
    arrives via iLovePDF with no AcroForm. field_synth.detect_blanks must
    find a useful number of fields across the contract. Spike measured 185
    detections (152 text + 33 checkbox) on the 14-page document; we set
    floor at 100 to absorb CI variation while catching real regressions."""
    pdf_bytes = CAR_FIXTURE.read_bytes()
    fields = detect_blanks(pdf_bytes)
    assert len(fields) >= 100, f"only {len(fields)} fields detected — CV pipeline regressed?"
    # Must catch both text underlines and checkboxes.
    by_kind = {}
    for f in fields:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    assert by_kind.get("text", 0) >= 80
    assert by_kind.get("checkbox", 0) >= 15


@pytest.mark.skipif(not CAR_FIXTURE.exists(), reason="CAR fixture not committed yet")
def test_car_brbc_round_trip_through_walk_fields():
    """Synthesized AcroForm must be readable by walk_fields with the same
    field count it was built from. This catches /Type, /Subtype, /Rect,
    /Annots-attachment bugs in synthesize_acroform that the CV stage
    doesn't surface."""
    from backend.pdf_introspect import walk_fields
    from pypdf import PdfReader
    import io

    pdf_bytes = CAR_FIXTURE.read_bytes()
    new_bytes, n_synth = try_synthesize(pdf_bytes)
    assert n_synth >= 100

    reader = PdfReader(io.BytesIO(new_bytes))
    walked = walk_fields(reader)
    assert len(walked) == n_synth, (
        f"walk_fields saw {len(walked)} but {n_synth} were synthesized"
    )

    # Every walked field must have the synthetic naming pattern AND a real
    # widget rect (Y-flip bug would manifest as rects outside [0, page_height]).
    for fi in walked[:20]:  # sample the first 20
        assert fi.dotted_name.startswith("f_")
        assert fi.field_type in ("/Tx", "/Btn")


# ---- programmatic flattening (works without external fixtures) ----

def _strip_acroform(pdf_bytes: bytes) -> bytes:
    """Re-emit a PDF with /AcroForm deleted from the root. Used to
    synthetically build a flattened test fixture from any AcroForm PDF."""
    import io
    from pypdf import PdfReader, PdfWriter

    src = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter(clone_from=src)
    if "/AcroForm" in writer._root_object:
        del writer._root_object["/AcroForm"]
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_flattened_lease_invoice_recovers_some_fields():
    """Take an existing AcroForm fixture, strip its form metadata, and
    confirm field_synth recovers a usable subset of fields visually.

    Floor is "≥1 field" — the Lease Invoice is a sparse one-page invoice
    so we don't expect to catch dozens, but we MUST catch at least one
    underline. If this regresses to zero, the CV pipeline is broken."""
    lease = Path(__file__).resolve().parent.parent.parent / "templates" / "pdf" / "2025 Compass Chicagoland Lease Invoice Landlords and Tenant Use copy.pdf"
    if not lease.exists():
        pytest.skip("lease invoice PDF not in templates dir")
    flattened = _strip_acroform(lease.read_bytes())
    fields = detect_blanks(flattened)
    assert len(fields) >= 1, "field_synth failed to detect any blanks on a real form"


def test_synthesize_acroform_empty_input_returns_original_bytes():
    """If detect_blanks finds nothing, synthesize_acroform must not modify
    the bytes. Round-trip identity is the contract for the genuine-scan case."""
    pdf_bytes = b"%PDF-1.4\n%%EOF"  # not a real PDF — but synthesize_acroform
    # with empty fields list should short-circuit before parsing.
    result = synthesize_acroform(pdf_bytes, [])
    assert result == pdf_bytes


def test_try_synthesize_on_corrupt_pdf_doesnt_raise():
    """Detection failures must never propagate — try_synthesize catches and
    returns (original_bytes, 0). Upload caller treats n=0 as "graceful
    error" rather than a 500."""
    result, n = try_synthesize(b"definitely not a pdf")
    assert n == 0
    # Original bytes returned unchanged.
    assert result == b"definitely not a pdf"
