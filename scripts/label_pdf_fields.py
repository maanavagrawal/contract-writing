"""
Label every text field in a template with a numeric ID and print a cheat
sheet mapping ID → field name. Tiny IDs fit into small fields cleanly,
so opening the labeled PDF instantly tells you which field name lives where.

Output:
  /tmp/labeled_<source>.pdf    — visual labels ("1", "2", ...)
  /tmp/labeled_<source>.txt    — cheat sheet (ID → field name)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader

from backend.pdf_fill import fill_pdf
from backend.pdf_introspect import walk_fields

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "pdf"
OUT_DIR = Path("/tmp")


def label(pdf_path: Path) -> tuple[Path, Path]:
    reader = PdfReader(str(pdf_path))
    fields = walk_fields(reader)

    text_values: dict[str, str] = {}
    cheat_lines: list[str] = []
    counter = 0
    for fi in fields:
        if fi.field_type != "/Tx":
            cheat_lines.append(f"     [{fi.field_type}] {fi.dotted_name}")
            continue
        counter += 1
        label_id = str(counter)
        text_values[fi.dotted_name] = label_id
        cheat_lines.append(f"{label_id:>4}  {fi.dotted_name}")

    pdf_bytes = fill_pdf(reader, text_values)

    out_pdf = OUT_DIR / f"labeled_{pdf_path.name}"
    out_pdf.write_bytes(pdf_bytes)

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
