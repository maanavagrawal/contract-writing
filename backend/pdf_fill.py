"""
Fill a PDF's AcroForm fields by writing /V directly on the field tree.

Replaces the old approach of pypdf.PdfWriter.update_page_form_field_values, which
silently fails when widget annotations don't carry /T (the field name lives on
the parent field instead — common in Compass/IL templates). The new walker
finds the leaf field by dotted-path name and writes /V there, plus /AS on the
widget kids for /Btn fields so checkboxes actually render checked.

The mapping value type: `str | SigStamp`. Text fields get a /V write; /Btn
fields branch on a leading "/" to pick state vs text; SigStamp values become
visual image overlays composited on top of the page via signature_stamp.
The pdf_fill walker collects SigStamp placements and apply_stamps runs once
at the end of fill_pdf.
"""
from __future__ import annotations

import io
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, IndirectObject, NameObject, NumberObject, TextStringObject

from .pdf_introspect import _deref
from .signature_stamp import SigStamp, StampPlacement, apply_stamps


def _any_signature_is_signed(writer: PdfWriter) -> bool:
    """Walk the AcroForm field tree; return True if any /Sig field has a /V
    (i.e. a real signature is present). Empty /Sig placeholders don't count."""
    catalog = writer._root_object
    if "/AcroForm" not in catalog:
        return False
    acroform = catalog["/AcroForm"]
    if hasattr(acroform, "get_object"):
        acroform = acroform.get_object()
    fields = acroform.get("/Fields")
    if fields is None:
        return False
    if isinstance(fields, IndirectObject):
        fields = _deref(fields)

    def _walk(node: Any) -> bool:
        obj = _deref(node)
        ft = obj.get("/FT")
        if ft is None:
            parent = obj.get("/Parent")
            if parent is not None:
                ft = _deref(parent).get("/FT")
        if str(ft) == "/Sig" and obj.get("/V") is not None:
            return True
        kids = obj.get("/Kids")
        if kids is not None:
            for k in _deref(kids):
                if _walk(k):
                    return True
        return False

    return any(_walk(f) for f in fields)


def _set_appearances_flag(writer: PdfWriter) -> None:
    """Tell viewers to regenerate appearance streams. Without this, Preview and
    some browsers show empty fields even when /V is set.

    Some IL contract templates ship with AcroForm /SigFlags=1 (SignaturesExist)
    pre-set even though no signature has actually been applied. With that bit
    set, Apple Preview and several browsers refuse to regenerate appearance
    streams (it would invalidate a non-existent signature), so all /Tx values
    and checkbox /AS states show as blank — the exact symptom users report.
    Clear the bit when no /Sig field is actually signed. Safe by definition:
    there's nothing to invalidate."""
    catalog = writer._root_object
    if "/AcroForm" not in catalog:
        return
    acroform = catalog["/AcroForm"]
    if hasattr(acroform, "get_object"):
        acroform = acroform.get_object()
    acroform[NameObject("/NeedAppearances")] = BooleanObject(True)
    if not _any_signature_is_signed(writer):
        sig_flags = int(acroform.get("/SigFlags", 0) or 0)
        if sig_flags & 1:
            acroform[NameObject("/SigFlags")] = NumberObject(sig_flags & ~1)


def _widget_page_index(widget_obj: Any, writer: PdfWriter) -> int | None:
    """Find which 1-based page a widget annotation lives on by searching
    each page's /Annots list for the same idnum. Returns None if not found
    (orphan widget — rare; safe to skip stamping in that case).

    Cached across one fill_pdf call via the caller; we don't cache globally
    because every fill_pdf creates a fresh PdfWriter with new idnums."""
    target_idnum = None
    try:
        # The widget may be reached via an IndirectObject; try to fetch its idnum.
        if hasattr(widget_obj, "indirect_reference") and widget_obj.indirect_reference is not None:
            target_idnum = widget_obj.indirect_reference.idnum
    except Exception:
        target_idnum = None

    for page_idx, page in enumerate(writer.pages, start=1):
        annots = page.get("/Annots")
        if annots is None:
            continue
        annots = _deref(annots)
        for annot_ref in annots:
            try:
                if isinstance(annot_ref, IndirectObject):
                    if annot_ref.idnum == target_idnum:
                        return page_idx
                annot_obj = _deref(annot_ref)
                if annot_obj is widget_obj:
                    return page_idx
            except Exception:
                continue
    return None


def _walk_and_fill(
    field_ref: Any,
    name_parts: list[str],
    rendered: dict[str, Any],
    writer: PdfWriter,
    placements: list[StampPlacement],
) -> None:
    """Mirror of pdf_introspect.walk_fields, but mutates /V (and /AS) instead of
    collecting metadata. Kept inline because the writer needs to mutate the
    cloned objects, and re-using introspect's leaf_obj from a different reader
    would be a footgun."""
    obj = _deref(field_ref)

    t = obj.get("/T")
    if t is not None:
        name_parts = name_parts + [str(t)]

    ft = obj.get("/FT")
    if ft is None:
        parent = obj.get("/Parent")
        if parent is not None:
            ft = _deref(parent).get("/FT")

    kids = obj.get("/Kids")
    if kids is not None:
        kids = _deref(kids)

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

    if field_kids:
        for fk in field_kids:
            _walk_and_fill(fk, name_parts, rendered, writer, placements)
        return

    # Leaf — does its dotted name match anything in the mapping?
    dotted = ".".join(name_parts)
    if dotted not in rendered:
        return
    value = rendered[dotted]
    if value == "":
        # Skip empties so we don't clobber a pre-filled field with blank.
        return

    # Signature stamp branch. We DON'T write /V (no text behind the image)
    # and we collect (page, rect, png) so apply_stamps can composite at the
    # end of fill_pdf. For /Sig fields we ALSO clear the widget appearance
    # so Reader doesn't draw the "Sign Here" caret on top of our stamp; the
    # widget is left in /Fields but with a /Ff 1 (ReadOnly) bit set so it
    # can't be clicked into edit mode.
    if isinstance(value, SigStamp):
        # The widget kids carry the /Rect we need to position the stamp.
        # Single-widget /Tx/Sig fields don't have widget_kids — the field
        # object IS the widget. Handle both.
        widget_sources: list[Any] = widget_kids if widget_kids else [field_ref]
        for w_ref in widget_sources:
            w = _deref(w_ref)
            rect = w.get("/Rect")
            if rect is None:
                continue
            try:
                llx, lly, urx, ury = (float(x) for x in rect)
            except Exception:
                continue
            page_idx = _widget_page_index(w, writer)
            if page_idx is None:
                continue
            placements.append(StampPlacement(
                page=page_idx,
                rect=(llx, lly, urx, ury),
                png_bytes=value.png_bytes,
            ))
            # For /Sig fields specifically: mark ReadOnly so Reader doesn't
            # offer "Click to sign" on top of our visual stamp. Bit 1 of /Ff.
            if str(ft) == "/Sig":
                cur_ff = int(obj.get("/Ff", 0) or 0)
                obj[NameObject("/Ff")] = NumberObject(cur_ff | 1)
                # Drop the appearance dict so the source's "Sign Here"
                # placeholder doesn't render under our stamp.
                if "/AP" in w:
                    del w["/AP"]
        return

    is_btn = (str(ft) == "/Btn") if ft else False
    looks_like_state = isinstance(value, str) and value.startswith("/")

    if is_btn and looks_like_state:
        state = NameObject(value)
        obj[NameObject("/V")] = state
        # For checkboxes the field IS the widget; for radio groups the kids are
        # the widgets. Write /AS on every widget kid that supports this state,
        # /Off on the rest.
        if widget_kids:
            for wk_ref in widget_kids:
                wk = _deref(wk_ref)
                ap = wk.get("/AP")
                supported_states: list[str] = []
                if ap is not None:
                    ap = _deref(ap)
                    n = ap.get("/N") if hasattr(ap, "get") else None
                    if n is not None:
                        n = _deref(n)
                        if hasattr(n, "keys"):
                            supported_states = [str(k) for k in n.keys()]
                if value in supported_states:
                    wk[NameObject("/AS")] = state
                else:
                    wk[NameObject("/AS")] = NameObject("/Off")
        else:
            # Single-widget /Btn — set /AS on the field itself.
            obj[NameObject("/AS")] = state
    else:
        obj[NameObject("/V")] = TextStringObject(value)


def fill_pdf(reader: PdfReader, rendered: dict[str, Any]) -> bytes:
    """Take a parsed PDF + a {field_name: value} dict, return the filled PDF
    as bytes. Empty values in `rendered` are skipped (don't overwrite
    existing /V with blank). Returns the raw bytes; caller decides where
    they go.

    Value type per field:
      - str starting with "/"          → /Btn state (checkbox/radio)
      - str otherwise                  → /V text write
      - SigStamp                       → composite PNG at the widget rect
                                         after the AcroForm pass

    Signature stamping happens AFTER /V writes because (a) the stamp is a
    visual overlay that doesn't interact with form state, and (b) doing it
    once at the end lets us merge all stamps in a single reportlab pass
    instead of per-field PDF rebuild."""
    writer = PdfWriter(clone_from=reader)

    catalog = writer._root_object
    placements: list[StampPlacement] = []

    if "/AcroForm" in catalog:
        acroform = catalog["/AcroForm"]
        if hasattr(acroform, "get_object"):
            acroform = acroform.get_object()
        fields = acroform.get("/Fields")
        if fields is not None:
            fields = _deref(fields) if isinstance(fields, IndirectObject) else fields
            for f in fields:
                _walk_and_fill(f, [], rendered, writer, placements)
            _set_appearances_flag(writer)

    # Serialize the AcroForm-mutated writer to bytes, then re-parse for the
    # stamp overlay merge. The two-step is necessary because reportlab's
    # overlay needs to know the source's page sizes, and the stamping
    # primitive operates on a PdfReader-shaped input.
    buf = io.BytesIO()
    writer.write(buf)
    filled_bytes = buf.getvalue()

    if not placements:
        return filled_bytes

    # Stamps requested → re-parse and composite. apply_stamps returns the
    # final bytes with the overlay merged onto each page.
    stamped_reader = PdfReader(io.BytesIO(filled_bytes))
    return apply_stamps(stamped_reader, placements)
