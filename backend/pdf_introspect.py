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

from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader
from pypdf.generic import IndirectObject


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
    dotted name matching what pypdf's get_fields() reports."""
    catalog = reader.trailer["/Root"]
    catalog = _deref(catalog)
    acroform = catalog.get("/AcroForm")
    if acroform is None:
        return []
    acroform = _deref(acroform)
    top_fields = acroform.get("/Fields")
    if top_fields is None:
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

    return out


def extract_neighbor_text(reader: PdfReader, page_num: int, rect: tuple[float, float, float, float], radius: float = 60.0) -> str:
    """Pull text near a widget rectangle. Used by the upload flow to give the
    AI mapper context like 'this field is labeled Tenant Email.'

    page_num: 1-based.
    rect: (llx, lly, urx, ury) in PDF user-space points.
    radius: how far from the rect to look (points). 60pt ≈ 1 inch.

    Returns a short, whitespace-collapsed string."""
    if page_num < 1 or page_num > len(reader.pages):
        return ""
    page = reader.pages[page_num - 1]

    llx, lly, urx, ury = rect
    expanded = (llx - radius, lly - radius, urx + radius, ury + radius)

    pieces: list[str] = []

    def visitor(text: str, cm, tm, font_dict, font_size) -> None:
        # tm is the text matrix; positions are tm[4], tm[5].
        try:
            x = float(tm[4])
            y = float(tm[5])
        except (TypeError, IndexError, ValueError):
            return
        if expanded[0] <= x <= expanded[2] and expanded[1] <= y <= expanded[3]:
            stripped = text.strip()
            if stripped:
                pieces.append(stripped)

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        # Some malformed PDFs blow up on extract_text. Better to return "" than crash.
        return ""

    return " ".join(pieces)[:500]
