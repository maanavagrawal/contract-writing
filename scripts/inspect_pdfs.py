"""
Walk every template PDF and report:
  - page count
  - whether it has an AcroForm (interactive fields)
  - if so: each field's name, type, and the page it lives on

Output drives the fill-strategy decision: AcroForm names → fill by name,
no AcroForm → coordinate-overlay templating.
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import NameObject

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "pdf"

FIELD_TYPE = {
    "/Tx": "text",
    "/Btn": "button/checkbox",
    "/Ch": "choice",
    "/Sig": "signature",
}


def page_index_of(reader: PdfReader, field_obj) -> int | None:
    """Return 1-based page number for a field annotation, or None."""
    for i, page in enumerate(reader.pages):
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot in annots:
            try:
                if annot.get_object() == field_obj.get_object():
                    return i + 1
            except Exception:
                continue
    return None


def inspect(pdf_path: Path) -> None:
    print(f"\n{'='*72}\n{pdf_path.name}\n{'='*72}")
    reader = PdfReader(str(pdf_path))
    print(f"pages: {len(reader.pages)}")

    fields = reader.get_fields()
    if not fields:
        print("AcroForm: NONE — needs coordinate overlay")
        return

    print(f"AcroForm: YES — {len(fields)} fields")
    print()
    print(f"  {'name':<55} {'type':<18} {'page':<5}")
    print(f"  {'-'*55} {'-'*18} {'-'*5}")
    for name, field in fields.items():
        ftype = FIELD_TYPE.get(field.get("/FT"), str(field.get("/FT") or "—"))
        page_num = "—"
        kids = field.get("/Kids")
        if kids:
            try:
                first_kid = kids[0]
                p = page_index_of(reader, first_kid)
                if p is not None:
                    page_num = str(p)
            except Exception:
                pass
        print(f"  {name[:55]:<55} {ftype:<18} {page_num:<5}")


def main() -> None:
    pdfs = sorted(TEMPLATES.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no PDFs in {TEMPLATES}")
    for pdf in pdfs:
        inspect(pdf)


if __name__ == "__main__":
    main()
