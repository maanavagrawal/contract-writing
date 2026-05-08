"""
Walk every template PDF and report:
  - page count
  - whether it has an AcroForm (interactive fields)
  - if so: each field's dotted name, type, and the page(s) it lives on

Output drives the fill-strategy decision: AcroForm names → fill by name,
no AcroForm → coordinate-overlay templating.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running this script directly (python scripts/inspect_pdfs.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader

from backend.pdf_introspect import walk_fields

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "pdf"

FIELD_TYPE = {
    "/Tx": "text",
    "/Btn": "button/checkbox",
    "/Ch": "choice",
    "/Sig": "signature",
}


def inspect(pdf_path: Path) -> None:
    print(f"\n{'='*72}\n{pdf_path.name}\n{'='*72}")
    reader = PdfReader(str(pdf_path))
    print(f"pages: {len(reader.pages)}")

    fields = walk_fields(reader)
    if not fields:
        print("AcroForm: NONE — needs coordinate overlay")
        return

    print(f"AcroForm: YES — {len(fields)} leaf fields")
    print()
    print(f"  {'name':<55} {'type':<18} {'page(s)':<10}")
    print(f"  {'-'*55} {'-'*18} {'-'*10}")
    for fi in fields:
        ftype = FIELD_TYPE.get(fi.field_type, fi.field_type or "—")
        pages = sorted({w.page for w in fi.widgets if w.page > 0})
        page_str = ",".join(str(p) for p in pages) if pages else "—"
        print(f"  {fi.dotted_name[:55]:<55} {ftype:<18} {page_str:<10}")


def main() -> None:
    pdfs = sorted(TEMPLATES.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no PDFs in {TEMPLATES}")
    for pdf in pdfs:
        inspect(pdf)


if __name__ == "__main__":
    main()
