"""
For Multi-Board: dump every widget annotation per page, sorted top-to-bottom
then left-to-right, with its qualified field name + rect.

This is the lookup table I correlate against the original contract text
(line-numbered) to build the mapping. No guessing.
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "templates" / "pdf" / "Multi-Board-8.0 (1).pdf"


def main() -> None:
    r = PdfReader(str(PDF))

    # Map ref id → qualified field name (so child widgets resolve to their parent /T).
    fields = r.get_fields() or {}
    ref_to_name: dict[int, str] = {}
    for name, f in fields.items():
        kids = f.get("/Kids")
        if kids:
            for k in kids:
                try:
                    ref_to_name[k.idnum] = name
                except Exception:
                    pass
        try:
            ref_to_name[f.indirect_reference.idnum] = name
        except Exception:
            pass

    for pi, page in enumerate(r.pages, 1):
        print(f"\n{'='*80}\nPAGE {pi}  ({page.mediabox.width:.0f} x {page.mediabox.height:.0f})\n{'='*80}")
        if "/Annots" not in page:
            continue
        rows = []
        for a in page["/Annots"]:
            o = a.get_object()
            if o.get("/Subtype") != "/Widget":
                continue
            rect = o.get("/Rect") or [0, 0, 0, 0]
            rect = [float(x) for x in rect]
            name = str(o.get("/T") or "")
            ft = str(o.get("/FT") or "")
            if not name:
                try:
                    name = ref_to_name.get(a.idnum, "?")
                except Exception:
                    pass
            # Walk parent for /FT if missing
            if not ft:
                p = o.get("/Parent")
                if p:
                    ft = str(p.get_object().get("/FT") or "")
            rows.append((name, ft, rect))
        # Sort top-to-bottom (high y first), then left-to-right
        rows.sort(key=lambda r: (-r[2][3], r[2][0]))
        for name, ft, rect in rows:
            x1, y1, x2, y2 = rect
            kind = {"/Tx": "Tx ", "/Btn": "Btn", "/Ch": "Ch ", "/Sig": "Sig"}.get(ft, "?  ")
            print(f"  y={y2:6.1f}  x={x1:6.1f}  w={x2-x1:6.1f}  {kind}  {name!r}")


if __name__ == "__main__":
    main()
