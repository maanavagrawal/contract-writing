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

from .interpolate import build_context, interpolate_mapping
from .pdf_fill import fill_pdf
from .schema import AgentProfile, GeneratedDoc, MappingFile, TransactionFields, UncertainField
from .signature_stamp import InvalidSignaturePng, SigStamp
from .templates import _is_handfill_extra

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
    template_extras: dict[str, dict[str, object]] | None = None,
) -> GeneratedDoc:
    mapping = _load_mapping(document_key)

    source_pdf = TEMPLATES_DIR / mapping.meta.source_pdf
    if not source_pdf.exists():
        raise FileNotFoundError(f"template PDF missing: {source_pdf}")

    # Pick this template's extras out of the per-template dict the request
    # carries. Mapping strings use "{template_extras.<name>}" (NOT keyed by
    # template id — the AI doesn't know its own template id at mapping time),
    # so we flatten just this template's slice into ctx.template_extras.
    extras_for_this_doc: dict[str, object] = {}
    if template_extras:
        per_template = template_extras.get(document_key)
        if isinstance(per_template, dict):
            extras_for_this_doc = per_template

    ctx = build_context(
        fields_dict=fields.model_dump(mode="json"),
        agent_dict=agent.model_dump(mode="json"),
        template_extras=extras_for_this_doc,
    )

    # interpolate_mapping handles both string templates and BtnChoice
    # conditional state lookups. Returns a flat {pdf_field: rendered_string}
    # dict ready for fill_pdf.
    rendered: dict[str, object] = dict(interpolate_mapping(mapping.fields, ctx))

    # Signature substitution. Any pdf_field whose mapping template was exactly
    # "{agent.signature}" or "{agent.initials}" gets its rendered string
    # value replaced with a SigStamp dataclass so pdf_fill stamps the PNG
    # instead of writing text. Detection is done on the source mapping
    # template (not the rendered value) because the rendered value would be
    # the literal base64 string and that's ambiguous with an actual base64
    # field value the user might have legitimately typed somewhere else.
    #
    # If the agent hasn't saved a signature (agent.signature is None), the
    # rendered string for those fields is empty — pdf_fill already skips
    # empties, so the field stays blank without further action.
    signature_b64 = (agent.signature or "").strip()
    initials_b64 = (agent.initials or "").strip()
    signature_fields_total = 0
    signature_fields_stamped = 0
    for pdf_field, raw_template in mapping.fields.items():
        if not isinstance(raw_template, str):
            continue
        template = raw_template.strip()
        if template == "{agent.signature}":
            signature_fields_total += 1
            if signature_b64:
                try:
                    rendered[pdf_field] = SigStamp(
                        kind="agent",
                        png_bytes=base64.b64decode(signature_b64, validate=True),
                    )
                    signature_fields_stamped += 1
                except Exception:
                    # Corrupt base64 in the stored default — fall through to
                    # blank field + soft warning. Never 500 on a bad PNG.
                    rendered[pdf_field] = ""
            else:
                rendered[pdf_field] = ""
        elif template == "{agent.initials}":
            signature_fields_total += 1
            if initials_b64:
                try:
                    rendered[pdf_field] = SigStamp(
                        kind="initials",
                        png_bytes=base64.b64decode(initials_b64, validate=True),
                    )
                    signature_fields_stamped += 1
                except Exception:
                    rendered[pdf_field] = ""
            else:
                rendered[pdf_field] = ""

    reader = PdfReader(str(source_pdf))
    try:
        pdf_bytes = fill_pdf(reader, rendered)
    except InvalidSignaturePng:
        # The PNG decoded fine here but reportlab choked on it at stamp
        # time — fall back to a fill with all signature fields blanked and
        # re-issue the call. Slow path but rare; corrupt-but-decodable
        # PNGs are an edge case worth handling without a 500.
        rendered = {
            k: ("" if isinstance(v, SigStamp) else v)
            for k, v in rendered.items()
        }
        pdf_bytes = fill_pdf(reader, rendered)
        # Reset stamp count — fallback wrote zero stamps. The
        # signature_fields_total still reflects what the template asked for,
        # so signature_status.fields_left_blank will surface correctly.
        signature_fields_stamped = 0

    # Surface low-confidence fields to the frontend so the user can see
    # what we left blank and decide whether to fill it by hand. The mapping
    # already blanked them via proposal_to_mapping_file at upload time.
    #
    # Suppress signing-time fields (initials, sign-dates, party-role checkboxes,
    # decorative items) — proposal_to_mapping_file added the suppress filter
    # at upload time, but apply the same filter here too so older mappings
    # (uploaded before the filter shipped) benefit without re-upload.
    uncertain: list[UncertainField] = []
    for lc in mapping.low_confidence or []:
        try:
            kind = lc["kind"]
            proposed = lc["proposed"]
            if kind == "extra" and _is_handfill_extra(proposed):
                continue
            uncertain.append(UncertainField(
                pdf_field=lc["pdf_field"],
                proposed=proposed,
                confidence=int(lc["confidence"]),
                kind=kind,
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
        signature_fields_total=signature_fields_total,
        signature_fields_stamped=signature_fields_stamped,
    )
