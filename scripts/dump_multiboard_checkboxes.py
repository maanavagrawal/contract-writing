"""
For every /Btn field in Multi-Board: print the on-state name (the value pypdf
needs to be set to in order to check the box).

Some checkboxes are radio groups (multiple widgets sharing one /T) — for those,
list each widget's distinct on-state.
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "templates" / "pdf" / "Multi-Board-8.0 (1).pdf"


def main() -> None:
    r = PdfReader(str(PDF))
    fields = r.get_fields() or {}

    # Resolve ref id → field name (for child widgets)
    ref_to_name: dict[int, str] = {}
    for name, f in fields.items():
        kids = f.get("/Kids")
        if kids:
            for k in kids:
                try:
                    ref_to_name[k.idnum] = name
                except Exception:
                    pass

    # For each /Btn field, find all its widget annotations and their AP /N states
    btn_field_states: dict[str, list[tuple[int, list[str]]]] = {}
    for pi, page in enumerate(r.pages, 1):
        if "/Annots" not in page:
            continue
        for a in page["/Annots"]:
            o = a.get_object()
            if o.get("/Subtype") != "/Widget":
                continue
            # walk to parent for /FT
            ft = str(o.get("/FT") or "")
            name = str(o.get("/T") or "")
            parent = o.get("/Parent")
            if not name and parent is None:
                try:
                    name = ref_to_name.get(a.idnum, "?")
                except Exception:
                    pass
            if not ft and parent:
                p_obj = parent.get_object()
                ft = str(p_obj.get("/FT") or "")
                if not name:
                    name = str(p_obj.get("/T") or "")
            if not name:
                try:
                    name = ref_to_name.get(a.idnum, "?")
                except Exception:
                    pass
            if ft != "/Btn":
                continue
            ap = o.get("/AP")
            states: list[str] = []
            if ap is not None:
                ap_obj = ap.get_object() if hasattr(ap, "get_object") else ap
                normal = ap_obj.get("/N")
                if normal is not None:
                    n_obj = normal.get_object() if hasattr(normal, "get_object") else normal
                    if hasattr(n_obj, "keys"):
                        states = [str(k) for k in n_obj.keys()]
            btn_field_states.setdefault(name, []).append((pi, states))

    print(f"{'field':>8} | {'pages':>10} | states")
    print("-" * 70)
    for name in sorted(btn_field_states, key=lambda s: (len(s), s)):
        widgets = btn_field_states[name]
        pages = sorted({pi for pi, _ in widgets})
        # Collapse states across all widgets — for radios, distinct states matter
        all_states = []
        for _, st in widgets:
            for s in st:
                if s not in all_states:
                    all_states.append(s)
        print(f"  {name!r:>8} | p{','.join(map(str,pages)):>9} | {all_states}")


if __name__ == "__main__":
    main()
