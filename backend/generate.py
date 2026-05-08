"""
Fill a PDF template's AcroForm fields from a TransactionFields + AgentProfile.

For each requested document:
  1. Load the mapping JSON (which PDF + which field-name → which template string)
  2. Build a context dict from the request payload
  3. Interpolate every mapping value
  4. Walk the AcroForm field tree, write /V on every leaf whose dotted name
     matches a mapping key, and /AS on widget kids for /Btn fields
  5. Return the filled bytes (base64)

The actual fill logic lives in pdf_fill.py; this module orchestrates mapping
load + interpolation + delivery.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from pypdf import PdfReader

from .interpolate import build_context, interpolate
from .pdf_fill import fill_pdf
from .schema import AgentProfile, GeneratedDoc, TransactionFields

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = Path(__file__).resolve().parent / "mappings"
TEMPLATES_DIR = ROOT / "templates" / "pdf"


class UnknownDocument(Exception):
    pass


def _load_mapping(document_key: str) -> dict:
    path = MAPPINGS_DIR / f"{document_key}.json"
    if not path.exists():
        raise UnknownDocument(f"no mapping found for '{document_key}'")
    return json.loads(path.read_text())


def fill_document(
    document_key: str,
    fields: TransactionFields,
    agent: AgentProfile,
) -> GeneratedDoc:
    mapping = _load_mapping(document_key)
    meta = mapping.get("_meta", {})
    field_templates: dict[str, str] = mapping.get("fields", {})

    source_pdf = TEMPLATES_DIR / meta["source_pdf"]
    if not source_pdf.exists():
        raise FileNotFoundError(f"template PDF missing: {source_pdf}")

    ctx = build_context(
        fields_dict=fields.model_dump(mode="json"),
        agent_dict=agent.model_dump(mode="json"),
    )

    rendered = {pdf_field: interpolate(tmpl, ctx) for pdf_field, tmpl in field_templates.items()}

    reader = PdfReader(str(source_pdf))
    pdf_bytes = fill_pdf(reader, rendered)

    return GeneratedDoc(
        document=document_key,
        filename=meta.get("filled_filename", f"{document_key}_filled.pdf"),
        base64=base64.b64encode(pdf_bytes).decode("ascii"),
    )
