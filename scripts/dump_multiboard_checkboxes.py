"""
For every /Btn field in Multi-Board: print the on-state name (the value pdf_fill
needs in order to check the box).

Some checkboxes are radio groups (multiple widgets sharing one /T) — for those,
list each widget's distinct on-state.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pypdf import PdfReader

from backend.pdf_introspect import walk_fields

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "templates" / "pdf" / "Multi-Board-8.0 (1).pdf"


def main() -> None:
    r = PdfReader(str(PDF))
    fields = [fi for fi in walk_fields(r) if fi.field_type == "/Btn"]

    print(f"{'field':>12} | {'pages':>10} | states")
    print("-" * 80)
    for fi in fields:
        pages = sorted({w.page for w in fi.widgets if w.page > 0})
        states: list[str] = []
        for w in fi.widgets:
            for s in w.states:
                if s not in states:
                    states.append(s)
        print(f"  {fi.dotted_name!r:>10} | p{','.join(map(str, pages)):>9} | {states}")


if __name__ == "__main__":
    main()
