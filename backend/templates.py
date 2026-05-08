"""
Template upload + AI-proposed mapping (Pillar 2).

Pipeline (all sync, fronted by /api/templates/upload):
  1. Persist the uploaded PDF to templates/pdf/<uuid>.pdf.
  2. Validate it has an AcroForm (we don't yet support coordinate-overlay
     templates) and isn't encrypted.
  3. Walk every field with pdf_introspect.walk_fields and grab the surrounding
     text via extract_neighbor_text. That neighbor text is what gives the AI
     the "this blank is labeled Tenant Email" signal.
  4. Ask GPT-5 to map each field to a canonical TransactionFields path, OR
     declare it a template-specific extra_field. Returns a MappingFile-shaped
     proposal.
  5. Write that proposal to backend/mappings/<uuid>.json and insert a
     Template row with status='pending_review' so the user knows to review
     it in the (forthcoming) UI before it goes live.

The AI is allowed to propose mappings for radio groups (/Btn with multi-state)
and dropdowns (/Ch). fill_pdf already handles them via state-name strings;
the frontend renders them read-only in the preview but they still autofill
from notes — best of both worlds without new code.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel, Field
from pypdf import PdfReader

from .models import (
    DEFAULT_USER_ID,
    ExtraField,
    Template,
    TemplateStatus,
    new_id,
    now_iso,
)
from .pdf_introspect import extract_neighbor_text, walk_fields
from .schema import MappingFile, MappingMeta

MODEL = "gpt-5"

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PDF_DIR = ROOT / "templates" / "pdf"
MAPPINGS_DIR = Path(__file__).resolve().parent / "mappings"


# ---------- AI proposal Pydantic shape (what GPT returns) ----------

class ProposedField(BaseModel):
    """One AI-proposed mapping row.

    Either canonical_path is set (the field maps to a known TransactionFields
    path like "property.address" or "lease_start") OR extra_field_name is set
    (the field is template-specific and gets stored under template_extras.<id>).
    Never both, never neither.
    """
    pdf_field: str                                 # the AcroForm field name (dotted)
    canonical_path: str | None = Field(None, description="A path into TransactionFields like 'property.address'. Null if this is a template-specific extra_field.")
    extra_field_name: str | None = Field(None, description="snake_case name if this is a template-specific extra. Null if canonical_path is set.")
    extra_field_type: str | None = Field(None, description="One of 'text','money','date','number','bool','list_str'. Required when extra_field_name is set.")
    extra_field_description: str | None = Field(None, description="One-line description used in the extraction prompt later.")


class ProposedMapping(BaseModel):
    """Top-level shape GPT returns. We translate it into MappingFile + ExtraField list."""
    fields: list[ProposedField]


# ---------- Errors raised by the upload pipeline ----------

class TemplateUploadError(Exception):
    """Anything that prevents accepting an uploaded PDF (no AcroForm,
    encrypted, malformed). Maps to 400 in the FastAPI handler."""


class AIMappingError(Exception):
    """GPT call failed or returned something we can't interpret. Maps to 502."""


# ---------- OpenAI plumbing ----------

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Lazy-init OpenAI client. Mirrors backend/extract.py's pattern."""
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise AIMappingError("OPENAI_API_KEY not set in environment")
        _client = OpenAI(api_key=api_key)
    return _client


# Keep this small and concrete — the schema description in the user message
# is what carries the field semantics.
SYSTEM_PROMPT = """\
You map AcroForm field names from a real-estate PDF onto a canonical
TransactionFields schema used by an Illinois real-estate paperwork tool.

For each PDF field you'll see:
  - the field's name (often a number or a label fragment)
  - the surrounding text in the PDF (the "neighbor text")
  - the field type (/Tx text, /Btn checkbox or radio, /Ch dropdown, /Sig signature)

You decide ONE of:
  A) The field maps to a canonical path in TransactionFields. Set canonical_path
     to the dotted path (e.g. "property.address", "lease_start", "monthly_rent",
     "tenant_or_buyer_names", "agent.name"). Leave extra_field_* fields null.

  B) The field is template-specific and not in the canonical schema. Set
     extra_field_name (snake_case identifier the frontend can show), set
     extra_field_type to one of [text, money, date, number, bool, list_str],
     and write a one-line extra_field_description that an AI extractor will
     later use to pull this value out of the agent's notes (e.g. "the pet's
     name as written"). Leave canonical_path null.

  C) The field is a signature (/Sig) or a separator the user fills by hand.
     Treat as a template-specific extra_field with type='text' and
     description='handwritten by signer at signing time'.

Rules:
  - Map every field. Do not skip any.
  - Prefer canonical paths when the neighbor text obviously matches a known
    field. "Tenant Email" → tenant_or_buyer_email. "Lease Start" → lease_start.
  - For radio groups (/Btn with multiple states) and dropdowns (/Ch), still
    map to canonical_path when sensible. The system handles state names later.
  - Date fields go to canonical date paths when obvious; otherwise extra_field
    with type=date.
  - Money fields with type=money. Counts/numbers with type=number.
  - When in doubt between A and B, prefer B (template-specific). The user
    reviews the proposal before it goes live.
"""


# Concise text version of TransactionFields. Keeping this static + small
# beats serializing the full Pydantic schema (which is verbose and burns
# tokens). Hand-edit when the canonical schema gains a new field.
CANONICAL_SCHEMA_HINT = """\
TransactionFields canonical paths (* = nullable):
  transaction_type*: 'lease' | 'sale'
  property.address*, property.unit*, property.city*, property.state*, property.zip*
  lease_start* (ISO date), lease_end* (ISO date), monthly_rent* (string),
    net_monthly_rent*, concessions*
  purchase_price*, closing_date* (ISO date), earnest_money*,
    earnest_business_days*, additional_earnest_money*, additional_earnest_date*,
    credit_at_closing*
  seller_names* (list), property_type*: 'attached'|'detached'|'multi_unit'
  county* (string), buyer_address*, buyer_city*, buyer_state*, buyer_zip*
  loan_type*: 'conventional'|'fha'|'va'|'usda'|'other'
  loan_type_other*, loan_rate_type*: 'fixed'|'adjustable'
  loan_percent_of_price*, loan_max_rate*, loan_amortization_years*, loan_max_points*
  tax_proration_percent*, hoa_fee*, hoa_frequency*: 'month'|'quarter'|'year'
  escrowee*: 'seller'|'buyer'|'other'
  tenant_or_buyer_names* (list), tenant_or_buyer_email*, tenant_or_buyer_phone*
  landlord_billing_email*, commission_amount*, retainer*
  protection_period_days*, early_termination_fee*

Agent profile (use as 'agent.<x>'):
  agent.name, agent.license, agent.brokerage, agent.brokerage_address,
  agent.brokerage_mls, agent.brokerage_license, agent.phone, agent.email,
  agent.mls
"""


# ---------- Public API ----------

def save_uploaded_pdf(pdf_bytes: bytes, template_id: str) -> Path:
    """Persist the uploaded PDF under templates/pdf/<id>.pdf. Returns the
    on-disk path so callers can record it on the Template row."""
    TEMPLATES_PDF_DIR.mkdir(parents=True, exist_ok=True)
    target = TEMPLATES_PDF_DIR / f"{template_id}.pdf"
    target.write_bytes(pdf_bytes)
    return target


def validate_pdf(pdf_bytes: bytes) -> PdfReader:
    """Open + sanity-check the PDF. Raises TemplateUploadError with a
    user-friendly message on anything that prevents fill: encryption, no
    AcroForm, or malformed bytes."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        raise TemplateUploadError(f"could not parse PDF: {e}")

    if reader.is_encrypted:
        raise TemplateUploadError(
            "this PDF is password-protected — unlock it before uploading"
        )

    fields = walk_fields(reader)
    if not fields:
        raise TemplateUploadError(
            "this PDF has no fillable form fields. We only support AcroForm-"
            "enabled templates today (no scans or flattened PDFs)."
        )
    return reader


def collect_field_descriptions(reader: PdfReader) -> list[dict]:
    """For each AcroForm field, build the dict we hand the AI: name, type,
    neighbor text, and rect coordinates so the AI knows roughly where on the
    page each field sits."""
    out: list[dict] = []
    for fi in walk_fields(reader):
        # Use the first widget's rect for neighbor-text. Hierarchical fields
        # with multiple widgets (rare) reuse the first. Good enough for the AI.
        first = fi.widgets[0] if fi.widgets else None
        neighbor = ""
        if first and first.rect and first.page > 0:
            neighbor = extract_neighbor_text(reader, first.page, first.rect)
        out.append({
            "pdf_field": fi.dotted_name,
            "field_type": fi.field_type,
            "neighbor_text": neighbor[:200],   # cap to keep token count sane
            "page": first.page if first else 0,
        })
    return out


async def propose_mapping(field_descriptions: list[dict]) -> ProposedMapping:
    """Ask GPT-5 to propose a mapping. Returns a ProposedMapping. Raises
    AIMappingError on API failure or empty parse.

    Run on a worker thread because OpenAI's SDK is blocking and this is hit
    from an async FastAPI route."""
    client = _get_client()

    # JSON-encode the field list as the user message; keeps tokens predictable
    # and the AI doesn't have to parse free text.
    user_msg = (
        f"{CANONICAL_SCHEMA_HINT}\n\n"
        f"Map every field below. Return a ProposedMapping.\n\n"
        f"FIELDS:\n{json.dumps(field_descriptions, indent=2)}"
    )

    try:
        response = await asyncio.to_thread(
            client.responses.parse,
            model=MODEL,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            text_format=ProposedMapping,
        )
    except Exception as e:
        raise AIMappingError(f"OpenAI API call failed: {e}")

    parsed = response.output_parsed
    if parsed is None:
        raise AIMappingError("OpenAI returned no parsed output")
    return parsed


def proposal_to_mapping_file(
    proposal: ProposedMapping,
    title: str,
    source_pdf_filename: str,
    filled_filename: str,
) -> tuple[MappingFile, list[ExtraField]]:
    """Convert the AI's ProposedMapping into the on-disk MappingFile shape +
    a separate ExtraField list for the templates table.

    For canonical paths the mapping value is '{<path>}' (template-engine
    syntax that interpolate.py already handles). For extras the value is
    '{template_extras.<template_id>.<extra_field_name>}', but since we don't
    yet wire dynamic-schema extraction into /api/extract (chunk 5), we render
    those as the bare extra_field reference and the form will fill them
    once chunk 5 lands.
    """
    fields: dict[str, str] = {}
    extras: list[ExtraField] = []

    for f in proposal.fields:
        if f.canonical_path:
            fields[f.pdf_field] = "{" + f.canonical_path + "}"
        elif f.extra_field_name:
            # Reference into template_extras. Even if the dynamic-schema
            # extractor isn't live yet, the user can edit the value in the
            # form on the left and the fill_pdf path still works.
            fields[f.pdf_field] = "{template_extras." + f.extra_field_name + "}"
            extras.append(ExtraField(
                name=f.extra_field_name,
                type=f.extra_field_type or "text",
                description=f.extra_field_description or "",
                pdf_field=f.pdf_field,
            ))
        else:
            # AI returned neither — treat as unmapped (empty string). User
            # fixes in the review UI.
            fields[f.pdf_field] = ""

    mapping = MappingFile(
        meta=MappingMeta(
            title=title,
            source_pdf=source_pdf_filename,
            filled_filename=filled_filename,
        ),
        fields=fields,
    )
    return mapping, extras


def write_mapping_file(mapping: MappingFile, template_id: str) -> Path:
    """Serialize the MappingFile to backend/mappings/<id>.json. Aliased '_meta'
    is preserved by Pydantic when we call model_dump(by_alias=True)."""
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    target = MAPPINGS_DIR / f"{template_id}.json"
    target.write_text(json.dumps(mapping.model_dump(by_alias=True), indent=2))
    return target


def _path_for_storage(p: Path) -> str:
    """Store paths relative to repo root when inside the repo (portable across
    dev environments), absolute when outside (test fixtures point at /tmp)."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p.resolve())


def build_template_row(
    template_id: str,
    title: str,
    source_pdf_path: Path,
    mapping_path: Path,
    extras: list[ExtraField],
    user_id: str,
    status: TemplateStatus = "pending_review",
) -> Template:
    """Construct a Template row. Paths are stored relative to repo root for
    portability when inside the repo, absolute otherwise."""
    return Template(
        id=template_id,
        user_id=user_id or DEFAULT_USER_ID,
        title=title,
        source_pdf_path=_path_for_storage(source_pdf_path),
        mapping_path=_path_for_storage(mapping_path),
        status=status,
        is_default=False,
        extra_fields=extras,
        created_at=now_iso(),
    )
