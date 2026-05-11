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
import os
from pathlib import Path

from pydantic import ValidationError
from pypdf import PdfReader

from .interpolate import build_context, interpolate
from .pdf_fill import fill_pdf
from .schema import AgentProfile, GeneratedDoc, MappingFile, TransactionFields, UncertainField

ROOT = Path(__file__).resolve().parent.parent

# Same STORAGE_DIR contract as backend/templates.py — keep both modules
# pointing at the same on-disk layout so save_uploaded_pdf and fill_document
# read/write through the same volume mount in production.
_STORAGE_ROOT = os.environ.get("STORAGE_DIR")
if _STORAGE_ROOT:
    MAPPINGS_DIR = Path(_STORAGE_ROOT) / "mappings"
    TEMPLATES_DIR = Path(_STORAGE_ROOT) / "pdf"
else:
    MAPPINGS_DIR = Path(__file__).resolve().parent / "mappings"
    TEMPLATES_DIR = ROOT / "templates" / "pdf"


class UnknownDocument(Exception):
    pass


class InvalidMapping(Exception):
    """Raised when a mapping JSON exists but doesn't conform to MappingFile.
    Surfaces a clear error instead of a Pydantic ValidationError to callers."""


def _load_mapping(document_key: str) -> MappingFile:
    """Load + validate a mapping JSON. Returns a Pydantic MappingFile so callers
    get typed access to meta + fields without poking at raw dicts.

    Path-traversal guard: document_key comes from request input. Reject any
    key containing path separators or leading dots so a payload like
    "../../etc/passwd" can't reach files outside MAPPINGS_DIR.
    """
    if "/" in document_key or "\\" in document_key or document_key.startswith("."):
        raise UnknownDocument(f"invalid document key: {document_key!r}")
    path = MAPPINGS_DIR / f"{document_key}.json"
    # Defense-in-depth: even with the char-class check above, resolve and
    # require the target stay inside MAPPINGS_DIR. Cheap insurance.
    try:
        path.resolve().relative_to(MAPPINGS_DIR.resolve())
    except ValueError:
        raise UnknownDocument(f"invalid document key: {document_key!r}")
    if not path.exists():
        raise UnknownDocument(f"no mapping found for '{document_key}'")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise InvalidMapping(f"mapping '{document_key}' is not valid JSON: {e}")
    try:
        return MappingFile.model_validate(raw)
    except ValidationError as e:
        # Compress the Pydantic error to one line per missing/wrong field;
        # full traces are noisy and the field paths are what callers want.
        issues = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise InvalidMapping(f"mapping '{document_key}' is invalid: {issues}")


def fill_document(
    document_key: str,
    fields: TransactionFields,
    agent: AgentProfile,
) -> GeneratedDoc:
    mapping = _load_mapping(document_key)

    source_pdf = TEMPLATES_DIR / mapping.meta.source_pdf
    if not source_pdf.exists():
        raise FileNotFoundError(f"template PDF missing: {source_pdf}")

    ctx = build_context(
        fields_dict=fields.model_dump(mode="json"),
        agent_dict=agent.model_dump(mode="json"),
    )

    rendered = {pdf_field: interpolate(tmpl, ctx) for pdf_field, tmpl in mapping.fields.items()}

    reader = PdfReader(str(source_pdf))
    pdf_bytes = fill_pdf(reader, rendered)

    # Surface low-confidence fields to the frontend so the user can see
    # what we left blank and decide whether to fill it by hand. The mapping
    # already blanked them via proposal_to_mapping_file at upload time.
    uncertain: list[UncertainField] = []
    for lc in mapping.low_confidence or []:
        try:
            uncertain.append(UncertainField(
                pdf_field=lc["pdf_field"],
                proposed=lc["proposed"],
                confidence=int(lc["confidence"]),
                kind=lc["kind"],
            ))
        except (KeyError, TypeError, ValueError):
            # Malformed entry from a hand-edited mapping JSON; skip silently
            # rather than 500 the whole generate call.
            continue

    return GeneratedDoc(
        document=document_key,
        filename=mapping.meta.filled_filename or f"{document_key}_filled.pdf",
        base64=base64.b64encode(pdf_bytes).decode("ascii"),
        uncertain_fields=uncertain,
    )
