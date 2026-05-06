"""
Label every text field in a template with a numeric ID and print a cheat
sheet mapping ID → field name. Tiny IDs fit into small fields cleanly,
so opening the labeled PDF instantly tells you which field name lives where.

Output:
  /tmp/labeled_<source>.pdf    — visual labels ("1", "2", ...)
  /tmp/labeled_<source>.txt    — cheat sheet (ID → field name)
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "pdf"
OUT_DIR = Path("/tmp")


def force_appearances(writer: PdfWriter) -> None:
    catalog = writer._root_object
    if "/AcroForm" not in catalog:
        return
    af = catalog["/AcroForm"]
    if hasattr(af, "get_object"):
        af = af.get_object()
    af[NameObject("/NeedAppearances")] = BooleanObject(True)


def label(pdf_path: Path) -> tuple[Path, Path]:
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter(clone_from=reader)

    fields = reader.get_fields() or {}
    # Number every text field. Stable order = whatever pypdf returns
    # (which corresponds to the AcroForm /Fields array order).
    text_values: dict[str, str] = {}
    cheat_lines: list[str] = []
    counter = 0
    for name, field in fields.items():
        ft = field.get("/FT")
        if ft != "/Tx":
            cheat_lines.append(f"     [{ft}] {name}")
            continue
        counter += 1
        label_id = str(counter)
        text_values[name] = label_id
        cheat_lines.append(f"{label_id:>4}  {name}")

    for page in writer.pages:
        if "/Annots" in page:
            writer.update_page_form_field_values(page, text_values, flatten=True)

    force_appearances(writer)

    out_pdf = OUT_DIR / f"labeled_{pdf_path.name}"
    with open(out_pdf, "wb") as f:
        writer.write(f)

    out_txt = OUT_DIR / f"labeled_{pdf_path.stem}.txt"
    out_txt.write_text("\n".join(cheat_lines) + "\n")

    return out_pdf, out_txt


def main() -> None:
    for pdf in sorted(TEMPLATES.glob("*.pdf")):
        out_pdf, out_txt = label(pdf)
        print(f"wrote {out_pdf}")
        print(f"  cheat sheet: {out_txt}")


if __name__ == "__main__":
    main()
