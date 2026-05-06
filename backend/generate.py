"""
Fill a PDF template's AcroForm fields from a TransactionFields + AgentProfile.

For each requested document:
  1. Load the mapping JSON (which PDF + which field-name → which template string)
  2. Build a context dict from the request payload
  3. Interpolate every mapping value
  4. Walk every page of the PDF and apply the values that exist on that page
  5. Set /NeedAppearances so all viewers re-render field appearances
  6. Return the filled bytes
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject

from .interpolate import build_context, interpolate
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


def _force_appearances(writer: PdfWriter) -> None:
    """Tell PDF viewers to regenerate field appearances on open. Without this,
    Preview / some browsers show empty fields even though the values are set.
    The /AcroForm reference may be wrapped in an IndirectObject — dereference
    before mutating."""
    catalog = writer._root_object
    if "/AcroForm" not in catalog:
        return
    acroform = catalog["/AcroForm"]
    if hasattr(acroform, "get_object"):
        acroform = acroform.get_object()
    acroform[NameObject("/NeedAppearances")] = BooleanObject(True)


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
    writer = PdfWriter(clone_from=reader)

    for page in writer.pages:
        # update_page_form_field_values silently ignores keys not on this page,
        # so it's safe to pass the full dict to every page.
        writer.update_page_form_field_values(page, rendered)

    _force_appearances(writer)

    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    import base64

    return GeneratedDoc(
        document=document_key,
        filename=meta.get("filled_filename", f"{document_key}_filled.pdf"),
        base64=base64.b64encode(pdf_bytes).decode("ascii"),
    )
