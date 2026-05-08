"""
Seed the 4 IL default templates into the templates table.

These map to the 4 mapping JSONs that already ship in backend/mappings/. We
register them with deterministic ids matching the mapping file stem, so
existing code paths that look up templates by `document_key` (e.g.
'lease_invoice') keep working and the seed is idempotent across re-runs.

is_default=1 marks them as un-deletable in the templates UI later.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

MAPPINGS_DIR = Path(__file__).resolve().parent.parent / "mappings"
TEMPLATES_PDF_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "pdf"

# Friendly titles per mapping file stem. Falls back to the JSON's _meta.title
# if missing here.
DEFAULTS = [
    "lease_invoice",
    "lease_abstract",
    "tenant_rep",
    "multiboard",
]


def run(conn: sqlite3.Connection) -> None:
    for tpl_id in DEFAULTS:
        mapping_path = MAPPINGS_DIR / f"{tpl_id}.json"
        if not mapping_path.exists():
            # Don't fail the migration if a default mapping is missing — the
            # admin can re-add it later. Just skip.
            continue
        mapping = json.loads(mapping_path.read_text())
        meta = mapping.get("_meta", {})
        title = meta.get("title", tpl_id)
        source_pdf_filename = meta.get("source_pdf", "")
        # Store the PDF path relative to repo root for portability.
        source_pdf_path = f"templates/pdf/{source_pdf_filename}"
        # Mapping path is relative to repo root too.
        mapping_rel = f"backend/mappings/{tpl_id}.json"

        # Confirm the source PDF actually exists on disk; otherwise we'd ship
        # broken default rows. Skip silently if missing.
        if not (TEMPLATES_PDF_DIR / source_pdf_filename).exists():
            continue

        conn.execute(
            """
            INSERT OR IGNORE INTO templates
                (id, title, source_pdf_path, mapping_path, status, is_default, extra_fields)
            VALUES (?, ?, ?, ?, 'ready', 1, '[]')
            """,
            (tpl_id, title, source_pdf_path, mapping_rel),
        )
