"""
Read AcroForm structure out of a PDF.

One module owns all "look at the PDF and tell me about its fields" logic:
  - Runtime fill (pdf_fill.py) needs the field tree to write /V on each leaf.
  - Upload flow (templates.py, Pillar 2) needs neighbor text for AI mapping.
  - scripts/* use the same primitives for one-off mapping authoring.

Why a custom walker instead of pypdf.PdfReader.get_fields(): get_fields() flattens
hierarchical names ("Lease End Date.0.0") but loses the parent → kid → widget
chain we need for two things: (1) writing /V on the right leaf when the same
dotted name has children, (2) reading widget /Rect + /AS off the kids.
"""
from __future__ import annotations

import re
import weakref
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader
from pypdf.generic import IndirectObject

# Process-local memoization of walk_fields. Three call sites hit this per
# upload (validate_pdf → collect_field_descriptions → collect_field_crops)
# and the walk is non-trivial on dense forms (~100-500ms on Multi-Board's
# 389 fields). WeakKeyDictionary so the cache evicts when the reader goes
# out of scope — no leak across requests.
_walk_cache: "weakref.WeakKeyDictionary[PdfReader, list[FieldInfo]]" = weakref.WeakKeyDictionary()


@dataclass
class WidgetInfo:
    page: int                       # 1-based page number, or 0 if unknown
    rect: tuple[float, float, float, float] | None  # (llx, lly, urx, ury) in PDF points
    states: list[str] = field(default_factory=list)  # /AS appearance state names ("/On", "/Choice1", ...)


@dataclass
class FieldInfo:
    dotted_name: str                # joined ancestor /T values, e.g. "Lease End Date.0.0"
    field_type: str                 # "/Tx" | "/Btn" | "/Ch" | "/Sig" | ""
    leaf_obj: Any                   # the underlying pypdf object so callers can mutate /V
    widgets: list[WidgetInfo] = field(default_factory=list)

    @property
    def states(self) -> list[str]:
        """Union of every widget's /AP/N keys, deduped, preserving first-seen order.
        For a checkbox: typically ['/Off', '/On']. For a radio group: the union
        of each kid's distinct state, e.g. ['/Off', '/Choice1', '/Choice2'].
        Empty for /Tx, /Sig, and widgets without an appearance dictionary.

        Used to tell the AI which literal state names a /Btn field accepts on
        THIS specific PDF — without this, the AI guesses '/On' and the fill
        engine silently writes '/Off' when the actual /AP/N key is something
        else like '/Yes' or '/Choice1'."""
        seen: list[str] = []
        for w in self.widgets:
            for s in w.states:
                if s not in seen:
                    seen.append(s)
        return seen


def _deref(obj: Any) -> Any:
    """Resolve IndirectObject → real object once. pypdf already does this lazily
    in many places, but explicit is better than guessing."""
    if isinstance(obj, IndirectObject) or hasattr(obj, "get_object"):
        try:
            return obj.get_object()
        except Exception:
            return obj
    return obj


def _build_widget_lookup(reader: PdfReader) -> tuple[dict[int, int], dict[str, int]]:
    """Walk every page's annotations once and build two lookup tables:
      - by_idnum: widget annotation idnum → 1-based page number (works when
        the widget annot IS the field object, common in source PDFs).
      - by_name:  joined /T path → 1-based page number. After pypdf clones
        the document, widgets and fields end up as separate objects with
        new idnums, but the widget still carries (or inherits) the field's
        /T. This fallback keeps page numbers correct in that case.
    """
    by_idnum: dict[int, int] = {}
    by_name: dict[str, int] = {}

    def collect_name(obj: Any) -> str:
        """Walk up /Parent chain joining /T values, mirroring walk_fields."""
        parts: list[str] = []
        cur: Any = obj
        seen: set[int] = set()
        while cur is not None:
            cur_obj = _deref(cur)
            try:
                cur_id = id(cur_obj)
                if cur_id in seen:
                    break
                seen.add(cur_id)
            except Exception:
                pass
            t = cur_obj.get("/T") if hasattr(cur_obj, "get") else None
            if t is not None:
                parts.append(str(t))
            cur = cur_obj.get("/Parent") if hasattr(cur_obj, "get") else None
        # We walked child→parent, so reverse for root→leaf naming.
        return ".".join(reversed(parts))

    for page_idx, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots")
        if annots is None:
            continue
        annots = _deref(annots)
        for annot_ref in annots:
            annot_obj = _deref(annot_ref)
            try:
                idnum = annot_ref.idnum if isinstance(annot_ref, IndirectObject) else annot_ref.indirect_reference.idnum
                by_idnum[idnum] = page_idx
            except Exception:
                pass
            # Only widgets carry form-field semantics.
            if annot_obj.get("/Subtype") != "/Widget":
                continue
            name = collect_name(annot_obj)
            if name and name not in by_name:
                by_name[name] = page_idx
    return by_idnum, by_name


def _extract_widget_states(widget_obj: Any) -> list[str]:
    """A /Btn widget's appearance dictionary lists the on-state names under /AP /N.
    For a checkbox: typically ["/Off", "/On"]. For a radio group: each kid has one
    distinct state ("/Choice1", "/Choice2", ...). For a regular text widget:
    nothing useful."""
    ap = widget_obj.get("/AP")
    if ap is None:
        return []
    ap = _deref(ap)
    n = ap.get("/N") if hasattr(ap, "get") else None
    if n is None:
        return []
    n = _deref(n)
    if not hasattr(n, "keys"):
        return []
    return [str(k) for k in n.keys()]


def _extract_widget_rect(widget_obj: Any) -> tuple[float, float, float, float] | None:
    rect = widget_obj.get("/Rect")
    if rect is None:
        return None
    try:
        return (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    except Exception:
        return None


def walk_fields(reader: PdfReader) -> list[FieldInfo]:
    """Recursively walk /AcroForm/Fields. Returns one FieldInfo per LEAF field
    (a field with /FT and no further children). Joins ancestor /T values into a
    dotted name matching what pypdf's get_fields() reports.

    Memoized per-reader (weak ref): three pipeline stages call this on the
    same upload — validate_pdf, collect_field_descriptions, collect_field_crops
    — and the walk itself is the most expensive non-AI step on dense forms.
    Cache hit returns the prior list immediately. If a caller actually
    mutates the AcroForm (templates.py field_synth path), they should call
    invalidate_walk_cache(reader) first.
    """
    cached = _walk_cache.get(reader)
    if cached is not None:
        return cached

    catalog = reader.trailer["/Root"]
    catalog = _deref(catalog)
    acroform = catalog.get("/AcroForm")
    if acroform is None:
        _walk_cache[reader] = []
        return []
    acroform = _deref(acroform)
    top_fields = acroform.get("/Fields")
    if top_fields is None:
        _walk_cache[reader] = []
        return []
    top_fields = _deref(top_fields)

    page_by_idnum, page_by_name = _build_widget_lookup(reader)
    out: list[FieldInfo] = []

    def page_for(ref: Any, dotted: str) -> int:
        try:
            idnum = ref.idnum if isinstance(ref, IndirectObject) else (
                ref.indirect_reference.idnum if hasattr(ref, "indirect_reference") else 0
            )
        except Exception:
            idnum = 0
        if idnum and idnum in page_by_idnum:
            return page_by_idnum[idnum]
        return page_by_name.get(dotted, 0)

    def visit(field_ref: Any, name_parts: list[str]) -> None:
        obj = _deref(field_ref)
        # Field name: /T may be missing on some intermediate nodes; that's ok.
        t = obj.get("/T")
        if t is not None:
            name_parts = name_parts + [str(t)]

        # /FT can be inherited from a parent. pypdf doesn't auto-resolve this when
        # we walk by hand, so check parents if missing.
        ft = obj.get("/FT")
        if ft is None:
            parent = obj.get("/Parent")
            if parent is not None:
                ft = _deref(parent).get("/FT")

        kids = obj.get("/Kids")
        if kids is not None:
            kids = _deref(kids)

        # Distinguish "real children that are themselves fields" from "widget annot kids."
        # A kid with its own /T is a named subfield (recurse). A widget without /T
        # is just an annotation belonging to this field. Lease Abstract has a
        # case where a kid is BOTH /Subtype=/Widget AND /T='0' AND /FT=/Tx — that's
        # a self-widgeted leaf field, treat as a field child.
        widget_kids: list[Any] = []
        field_kids: list[Any] = []
        if kids:
            for k in kids:
                k_obj = _deref(k)
                has_t = k_obj.get("/T") is not None
                has_ft = k_obj.get("/FT") is not None
                has_kids = k_obj.get("/Kids") is not None
                if has_t or has_kids or has_ft:
                    field_kids.append(k)
                else:
                    widget_kids.append(k)

        # If there are field children, recurse into them. This node itself is not a leaf.
        if field_kids:
            for fk in field_kids:
                visit(fk, name_parts)
            return

        # This is a leaf. Collect its widgets.
        dotted = ".".join(name_parts)
        widget_infos: list[WidgetInfo] = []
        if widget_kids:
            for wk_ref in widget_kids:
                wk = _deref(wk_ref)
                widget_infos.append(WidgetInfo(
                    page=page_for(wk_ref, dotted),
                    rect=_extract_widget_rect(wk),
                    states=_extract_widget_states(wk),
                ))
        else:
            # Field IS its own widget (the common case for PDFs where /T sits on
            # the parent and the widget annot has no separate /T).
            widget_infos.append(WidgetInfo(
                page=page_for(field_ref, dotted),
                rect=_extract_widget_rect(obj),
                states=_extract_widget_states(obj),
            ))

        out.append(FieldInfo(
            dotted_name=dotted,
            field_type=str(ft) if ft else "",
            leaf_obj=obj,
            widgets=widget_infos,
        ))

    for f in top_fields:
        visit(f, [])

    _walk_cache[reader] = out
    return out


def invalidate_walk_cache(reader: PdfReader) -> None:
    """Drop the cached walk_fields result for this reader. Call after any
    code path that mutates /AcroForm/Fields — currently only the synthetic
    AcroForm path in templates.field_synth, where the reader is rebuilt from
    new bytes and the original reader becomes stale anyway. Cheap safety
    net: a stale cache return would silently miss synthesized fields."""
    _walk_cache.pop(reader, None)


_LINE_NUM_RX = re.compile(r"^\s*\d{1,3}\s*$")


def _is_line_number_noise(text: str) -> bool:
    """Multi-Board and similar legal forms have left-margin line numbers
    ('1', '2', '20', '367', etc.) baked into the page text. They sit just
    left of the actual content and pollute LEFT context. Filter them out."""
    return bool(_LINE_NUM_RX.match(text))


def extract_neighbor_text(
    reader: PdfReader,
    page_num: int,
    rect: tuple[float, float, float, float],
    radius: float = 60.0,
) -> str:
    """Pull text that's most likely to be the LABEL for an AcroForm field.

    Real-world form labels live in one of four places relative to the input rect:
      1. LEFT on the same line ("Tenant Email: ___")
      2. ABOVE the field, often centered ("Lease End Date\\n___")
      3. RIGHT on the same line, for checkboxes ("□ Single Family Detached")
      4. BELOW the field as a column header — common in multi-column legal
         forms like Multi-Board where line 8 has six adjacent inputs labeled
         "Address | Unit # | City | State | Zip | County" on the row UNDER
         the rects, not above (form-line-9 in the printed page).

    A simple radius box (the v1 approach) drowns out the label with paragraph
    text on dense legal contracts. Each band is narrow on purpose: ~half a
    line tall vertically, ~3 inches wide horizontally. This stops adjacent
    rows of inputs (Buyer Name / Seller Name on lines 2-3 of Multi-Board)
    from leaking each other's labels into LEFT.

    page_num: 1-based.
    rect: (llx, lly, urx, ury) in PDF user-space points.
    radius: legacy parameter, ignored. Kept for caller compatibility.

    Returns up to ~400 chars of "LEFT: <text> | ABOVE: <text> | RIGHT: <text>
    | BELOW: <text>" — sections omitted when empty.
    """
    _ = radius  # kept for backwards-compat, no longer used
    if page_num < 1 or page_num > len(reader.pages):
        return ""
    page = reader.pages[page_num - 1]

    # Some PDFs store widget rects with reversed y (lly > ury). Normalize so
    # all our band math assumes the canonical (llx,lly) = bottom-left,
    # (urx,ury) = top-right convention.
    llx, lly, urx, ury = rect
    if lly > ury:
        lly, ury = ury, lly
    if llx > urx:
        llx, urx = urx, llx
    rect_h = max(ury - lly, 8.0)            # treat very thin checkboxes as ~8pt tall
    line_height = max(rect_h, 12.0) * 1.4   # typical line height with some headroom

    # Same-line band is rect-relative (not line-height-relative): it accepts
    # text whose baseline sits inside the rect or barely above/below it.
    # On a 14pt-tall input rect: band ~ [lly-2, ury+3], ~19pt total. Text
    # on the previous form line (e.g. Multi-Board's Buyer-Name label one
    # row above the Seller-Name input) has a baseline at ury + ~10pt which
    # is outside the band — no leak. On an 8pt-tall checkbox: band ~ [lly-1,
    # ury+2], ~11pt total. Checkbox labels typically have their baseline at
    # the rect's vertical center (well inside) so RIGHT detection still
    # works on the "Single Family Attached / Detached / Multi-Unit" cluster.
    # Caught by adversarial review on 2026-05-11.
    same_line_top = ury + rect_h * 0.25
    same_line_bot = lly - rect_h * 0.15

    above_top = ury + line_height * 1.6
    above_bot = ury + line_height * 0.4

    # BELOW band: captures the SINGLE line of text immediately under the
    # rect — the typical home of column-header labels in multi-column legal
    # forms (Multi-Board's address row labels: "Address | Unit # | City |
    # State | Zip | County"). Capped at ~0.9 line-heights so we don't reach
    # into the next paragraph's body text. Earlier draft used 1.6 line-heights
    # and pulled in paragraph fragments that the AI then over-trusted because
    # the prompt elevated "short BELOW" strings to primary labels (caught
    # by adversarial review 2026-05-11 — F2/F8).
    below_top = lly - line_height * 0.2
    below_bot = lly - line_height * 0.9

    left_pieces: list[tuple[float, str]] = []
    above_pieces: list[tuple[float, float, str]] = []
    right_pieces: list[tuple[float, str]] = []
    below_pieces: list[tuple[float, float, str]] = []

    def visitor(text: str, cm, tm, font_dict, font_size) -> None:
        try:
            x = float(tm[4])
            y = float(tm[5])
        except (TypeError, IndexError, ValueError):
            return
        stripped = text.strip()
        if not stripped or _is_line_number_noise(stripped):
            return

        if same_line_bot <= y <= same_line_top:
            # Left of the rect, within ~3 inches.
            if x < llx and x > llx - 220:
                left_pieces.append((x, stripped))
            # Right of the rect, within ~4 inches. Wider than LEFT because
            # checkbox labels can be far away on multi-column forms (e.g.
            # Multi-Board's "Single Family Attached / Detached / Multi-Unit").
            elif x > urx and x < urx + 280:
                right_pieces.append((x, stripped))
        elif above_bot < y <= above_top:
            # Allow some horizontal slack — labels above can be centered.
            if (llx - 60) <= x <= (urx + 60):
                above_pieces.append((y, x, stripped))
        elif below_bot < y <= below_top:
            # Column-header labels are typically narrow and centered under
            # one rect. Keep the horizontal slack the same as ABOVE so a
            # label that's slightly off-center still attaches to the right
            # field. Tighter than ABOVE wouldn't survive 1-2pt of design drift.
            if (llx - 60) <= x <= (urx + 60):
                below_pieces.append((y, x, stripped))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        return ""

    left_text = " ".join(t for _, t in sorted(left_pieces, key=lambda p: p[0]))[-180:]
    above_text = " ".join(t for _, _, t in sorted(above_pieces, key=lambda p: (-p[0], p[1])))[-180:]
    right_text = " ".join(t for _, t in sorted(right_pieces, key=lambda p: p[0]))[:160]
    # BELOW sorted top-to-bottom (largest y first) then left-to-right, mirroring
    # ABOVE's reading order so the closest label-row comes first.
    below_text = " ".join(t for _, _, t in sorted(below_pieces, key=lambda p: (-p[0], p[1])))[:180]

    parts: list[str] = []
    if left_text:
        parts.append(f"LEFT: {left_text}")
    if above_text:
        parts.append(f"ABOVE: {above_text}")
    if right_text:
        parts.append(f"RIGHT: {right_text}")
    if below_text:
        parts.append(f"BELOW: {below_text}")
    return " | ".join(parts)[:400]
