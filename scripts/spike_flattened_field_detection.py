"""
Spike: detect blank fields in a flattened CAR PDF using OpenCV.

Pipeline:
  1. Render page 1 (and page 3 — the one with the signature/initial table) to PNG.
  2. Run morphological operations + line/contour detection to find:
     - Long horizontal underlines (text fields)
     - Small rectangular outlines (checkboxes)
  3. Convert pixel coords back to PDF user-space coords.
  4. Output a visual debug overlay so we can eyeball the recall.

This is throwaway. The goal is one number: percentage of human-visible blanks
captured. >70% = production version is worth writing. <50% = we need a paid
vision service.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pypdfium2

PDF_PATH = "/Users/maanavagrawal/Downloads/_Buyer_Representation_and_Broker_Compensation_Agreement___12_25_unlocked.pdf"
OUTPUT_DIR = Path("/tmp/flattened_spike")
OUTPUT_DIR.mkdir(exist_ok=True)

# Render at this DPI. Higher = more detail, slower. 200 is a reasonable balance
# for line detection — at 72 DPI a 1pt-wide line is sub-pixel and we miss it.
RENDER_DPI = 200
SCALE = RENDER_DPI / 72.0  # PDF native is 72 DPI


def render_page(pdf_path: str, page_idx: int) -> tuple[np.ndarray, float, float]:
    """Render one page to a BGR numpy array. Returns (img, page_width_pt, page_height_pt)."""
    pdf = pypdfium2.PdfDocument(pdf_path)
    page = pdf[page_idx]
    w_pt = page.get_width()
    h_pt = page.get_height()
    bitmap = page.render(scale=SCALE, rotation=0)
    pil = bitmap.to_pil()
    bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return bgr, w_pt, h_pt


def detect_underlines(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find long horizontal lines — the underline-style text fields.

    Strategy:
      - Binarize (inverted so lines are white on black)
      - Morphological opening with a long horizontal kernel keeps only
        line-like structures
      - Find contours, filter by aspect ratio (very wide, short) and minimum width
    """
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    # Horizontal kernel: ~50px wide at 200 DPI = ~18pt wide minimum line
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (50, 1))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=1)

    contours, _ = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # Aspect: very wide vs short. Min 50px wide. Max ~12px tall (200dpi = 12pt thick)
        if w < 50 or h > 12:
            continue
        if w / max(h, 1) < 8:
            continue
        out.append((x, y, w, h))
    return out


def detect_checkboxes(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Find small square outlines — checkboxes.

    Strategy:
      - Binarize
      - findContours, look for nearly-square shapes of plausible checkbox size
        (8-24pt = 22-66px at 200 DPI)
      - Approximate the contour to 4 vertices to filter non-rectangles
    """
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # Size filter: roughly 22-66px square at 200 DPI
        if not (20 <= w <= 70 and 20 <= h <= 70):
            continue
        # Aspect: must be near-square (max 1.3x ratio)
        if max(w, h) / max(min(w, h), 1) > 1.3:
            continue
        # Must have hollow center: the contour should approximate to 4 corners
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.04 * peri, True)
        if len(approx) != 4:
            continue
        # Area sanity: real checkbox borders are thin, so the bounding box
        # area should be much larger than the contour's filled area would be.
        # We just check the contour isn't itself a filled rectangle by computing
        # the "openness" — ratio of contour perimeter² to area.
        area = cv2.contourArea(c)
        if area < 100:  # too small
            continue
        out.append((x, y, w, h))
    return out


def dedupe_rects(rects: list[tuple[int, int, int, int]], tol: int = 5) -> list[tuple[int, int, int, int]]:
    """Naive O(n²) dedupe — same upper-left and same size = duplicate."""
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


def analyze_page(page_idx: int) -> dict:
    """Run the full pipeline on one page. Save debug overlay. Return counts."""
    img, w_pt, h_pt = render_page(PDF_PATH, page_idx)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    underlines = detect_underlines(gray)
    checkboxes = detect_checkboxes(gray)
    underlines = dedupe_rects(underlines)
    checkboxes = dedupe_rects(checkboxes)

    # Draw overlay for visual verification.
    overlay = img.copy()
    for x, y, w, h in underlines:
        cv2.rectangle(overlay, (x, y - 2), (x + w, y + h + 2), (0, 0, 255), 2)
    for x, y, w, h in checkboxes:
        cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 200, 0), 2)
    out_path = OUTPUT_DIR / f"page_{page_idx + 1}_detected.png"
    cv2.imwrite(str(out_path), overlay)

    return {
        "page": page_idx + 1,
        "page_size_pt": (w_pt, h_pt),
        "underlines_found": len(underlines),
        "checkboxes_found": len(checkboxes),
        "overlay_path": str(out_path),
    }


def main():
    print(f"PDF: {PDF_PATH}\n")
    # Page 1: agency-disclosure header with name/date fields + checkboxes
    # Page 3: the BRBC main body — many checkboxes + underlines (densest page)
    # Page 9 (idx 8): the signature/initial table
    for idx in [0, 2, 8]:
        result = analyze_page(idx)
        print(f"Page {result['page']}: {result['underlines_found']} underlines, "
              f"{result['checkboxes_found']} checkboxes detected")
        print(f"  Overlay: {result['overlay_path']}")
    print(f"\nOpen the overlays:")
    print(f"  open {OUTPUT_DIR}/page_*_detected.png")


if __name__ == "__main__":
    main()
