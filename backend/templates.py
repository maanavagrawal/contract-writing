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
    ExtraField,
    Template,
    TemplateStatus,
    new_id,
    now_iso,
)
from .pdf_introspect import extract_neighbor_text, walk_fields
from .schema import BtnChoice, MappingFile, MappingMeta

MODEL = "gpt-5"

ROOT = Path(__file__).resolve().parent.parent

# Storage paths. In production (Railway) STORAGE_DIR points at a mounted
# volume so uploaded PDFs and mapping JSONs survive redeploys. Without a
# volume, both subdirs go to ephemeral disk and user uploads vanish on
# every push — fine for local dev (we use repo-relative paths there) but
# fatal in prod.
#
# Resolution order:
#   1. STORAGE_DIR env var (Railway sets this to /app/storage)
#   2. Repo-relative fallback (templates/pdf + backend/mappings)
#
# The two subdirs are pinned to specific names so mappings on disk match
# what tests + existing seeded fixtures expect.
_STORAGE_ROOT = os.environ.get("STORAGE_DIR")
if _STORAGE_ROOT:
    TEMPLATES_PDF_DIR = Path(_STORAGE_ROOT) / "pdf"
    MAPPINGS_DIR = Path(_STORAGE_ROOT) / "mappings"
else:
    TEMPLATES_PDF_DIR = ROOT / "templates" / "pdf"
    MAPPINGS_DIR = Path(__file__).resolve().parent / "mappings"


# ---------- AI proposal Pydantic shape (what GPT returns) ----------

class ProposedField(BaseModel):
    """One AI-proposed mapping row.

    Either canonical_path is set (the field maps to a known TransactionFields
    path like "property.address" or "lease_start") OR extra_field_name is set
    (the field is template-specific and gets stored under template_extras.<id>).
    Never both, never neither.

    confidence is a 1-10 score the AI assigns to its own decision. Fields with
    confidence < 7 get blanked out at fill time and surfaced to the user as
    "we weren't sure, you fill these in" — better than silently shipping a
    wrong value into a $1M contract. The threshold matters: at scale on a
    389-field form, even a 95%-accurate AI produces ~20 wrong fields, so
    self-reported uncertainty is the cheapest way to convert wrong-fills
    into blank-fills.
    """
    pdf_field: str                                 # the AcroForm field name (dotted)
    canonical_path: str | None = Field(None, description="A path into TransactionFields like 'property.address'. Null if this is a template-specific extra_field.")
    extra_field_name: str | None = Field(None, description="snake_case name if this is a template-specific extra. Null if canonical_path is set.")
    extra_field_type: str | None = Field(None, description="One of 'text','money','date','number','bool','list_str'. Required when extra_field_name is set.")
    extra_field_description: str | None = Field(None, description="One-line description used in the extraction prompt later.")
    confidence: int = Field(5, ge=1, le=10, description="1-10 confidence in this mapping. Use 9-10 only when the label is unambiguous (e.g. 'Buyer Name' next to a /Tx field). Use 1-4 when guessing from sparse or visual-only signal.")
    # /Btn-specific: when this field is a checkbox/radio AND a canonical path
    # exists, the AI emits btn_choices mapping each canonical_path value to the
    # widget's exact /AP/N state name. Example: for property_type checkbox
    # 'Single Family Attached' with widget states ['/Off','/On'], emit
    # btn_choices={"attached": "/On"} (or {"attached": "/On", "detached": "/Off",
    # "multi_unit": "/Off"} for the same field if it were a 3-way radio). The
    # interpolator resolves ctx[canonical_path] → choices[value] → state at
    # fill time. Defaults to '/Off' when value is missing or unknown.
    btn_choices: dict[str, str] | None = Field(
        None,
        description="For /Btn fields with a canonical_path: dict from canonical value (the value of ctx[canonical_path] as a string) to the widget's literal /AP/N state name. Omit for non-/Btn fields or when canonical_path is null.",
    )


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
  - neighbor_text: structured as "LEFT: <words to the left on the same line>
    | ABOVE: <line just above> | RIGHT: <words to the right on the same line>
    | BELOW: <column-header label on the line below>" (sections are omitted
    when empty). BELOW is the dominant signal on multi-column legal forms
    where labels print UNDER their input rects (e.g. an address row whose
    inputs are labeled "Address | Unit # | City | State | Zip" on the
    following line).
  - the field type (/Tx text, /Btn checkbox or radio, /Ch dropdown, /Sig signature)
  - states (ONLY for /Btn and /Ch): the literal /AP/N appearance-state names
    the widget will accept. A checkbox typically shows ["/Off", "/On"]; a
    radio group shows ["/Off", "/Choice1", "/Choice2", ...]. THESE ARE PER-PDF
    AND YOU MUST USE THE EXACT STRINGS GIVEN — do NOT invent "/On" if the
    list says ["/Off", "/Yes"]. See the btn_choices field below.
  - for some fields with empty neighbor_text: an attached cropped image of the
    field's surrounding area on the PDF page. Use the image to read labels
    that text extraction missed (column headers, table rows, hand-tagged
    boxes). The image is your primary signal when neighbor_text is empty.

LEFT text is the default label for single-column input fields. RIGHT text
is almost always the label for checkboxes. ABOVE text is often a section
heading or paragraph context. BELOW text occasionally contains the
column-header label on multi-column forms (e.g. Multi-Board's address row
where labels like "City" sit under their inputs) but often just contains
the start of the next paragraph — weigh BELOW against LEFT/ABOVE rather
than trusting it blindly. Don't trust the field's own name as a label,
since on legal forms fields are often just numbers ("1", "2", "112").

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
    map to canonical_path when sensible.
  - Date fields go to canonical date paths when obvious; otherwise extra_field
    with type=date.
  - Money fields with type=money. Counts/numbers with type=number.
  - When in doubt between A and B, **prefer A (canonical)**. An extra_field
    that isn't populated by the agent's transaction notes renders blank on
    the filled PDF — the user sees an empty form. A canonical path always
    has data behind it (agent profile + transaction notes).

  CRITICAL: synthetic field names like 'f_NNN_NNN' (page_NNN, index_NNN)
  come from PDFs that originally had no form fields and were detected
  visually. The field NAME has no meaning at all — your ONLY signal is
  the neighbor_text plus the cropped image. For these fields the canonical
  mapping is even more important because we have no template_extras pulled
  from the agent's notes for them.

  Common neighbor-text → canonical mappings to PREFER (do not invent
  template_extras when one of these applies):
    "Buyer" / "Print Buyer" / "Buyer Name"     → tenant_or_buyer_names
    "Seller" / "Print Seller" / "Seller Name"  → seller_names
    "Agent" / "By" + "DRE Lic" / "Broker/Agent" → agent.name
    "Real Estate Broker (Firm)" / "Brokerage"  → agent.brokerage
    "DRE Lic #" / "License Number"             → agent.license
    "MLS #"                                    → agent.mls
    "Address" + "City" + "State" + "Zip"       → property.address (and unit/city/etc)
    "Phone"                                    → agent.phone (when broker) or
                                                  tenant_or_buyer_phone (when buyer)
    "E-mail" / "Email"                         → agent.email or tenant_or_buyer_email
    "Date"                                     → today (always — the form fills
                                                  with the date of generation)
    "Purchase Price"                           → purchase_price
    "Earnest Money"                            → earnest_money
    "Closing"                                  → closing_date
    "Representation Period Beginning"          → today
    "Representation Period Ending"             → (extra_field, type=date)
    "% of acquisition price" / "Amount of Compensation" → commission_amount
    "Buyer Initial" / "Initial"                → (extra_field, type=text,
                                                  description='agent initials')
    Form titles, page numbers, footers, "Produced with..." watermarks
                                               → (extra_field, type=text,
                                                  description='static page text')

For /Btn fields ONLY:
  - When you set canonical_path on a /Btn field, you MUST ALSO emit btn_choices.
    Without btn_choices, the system has no way to know which state name to
    write to the widget — the result is a permanently blank checkbox.
  - btn_choices is a dict mapping canonical VALUES to the widget's exact /AP/N
    state names from the `states` field. Example: if canonical_path is
    "property_type" (enum: attached|detached|multi_unit) and this is the
    "Single Family Attached" checkbox with states ["/Off","/On"], emit:
      btn_choices = {"attached": "/On"}
    The system fills /Off when the canonical value doesn't match any key.
  - For a true RADIO group (one field, multiple kids, multiple states), enumerate
    every (canonical_value → state) the field encodes. Example: escrowee radio
    with states ["/Off","/Seller's Brokerage","/Buyer's Brokerage","/As otherwise agreed"]
    and canonical_path="escrowee" (enum: seller|buyer|other) gets:
      btn_choices = {"seller": "/Seller's Brokerage",
                     "buyer": "/Buyer's Brokerage",
                     "other": "/As otherwise agreed"}
  - For a TEMPLATE_EXTRAS boolean (a single checkbox without a canonical path),
    use canonical_path = "template_extras.<name>" and
    btn_choices = {"true": "/On"} (or the actual on-state from `states`).
  - Every value in btn_choices MUST be one of the strings in `states`. The
    system rejects mismatches and falls back to /Off. Do not invent state names.

Confidence (1-10): score every field. This drives a downstream gate that
BLANKS fields with confidence < 7 at fill time so wrong values don't ship
to users on legal contracts.

  10 — The neighbor text or visual image literally says the canonical
       field's standard name. "Buyer Name(s)" -> tenant_or_buyer_names.
       "Closing Date" -> closing_date. Zero ambiguity.
  8-9 — Label clearly maps to a canonical field, no other plausible
       reading even if the words aren't identical. "Purchase Price is
       $___" -> purchase_price. "Date of Acceptance" -> today.
  6-7 — Label is suggestive but you're inferring. PASSING the gate
       threshold (7) requires you to defend the inference in one sentence.
       Empty neighbor text + visible image shows "loan for ___%" near a
       FINANCING section -> loan_percent_of_price (score 7).
  4-5 — You have a guess but a competing canonical path is also plausible.
       A standalone "$" with no surrounding label could be earnest_money,
       purchase_price, credit_at_closing, or commission_amount. Score 4-5.
  1-3 — Pure positional or sequence reasoning. "Field 28 sits between
       fields about Earnest Money so probably additional_earnest_money."
       This is a hypothesis, not a mapping. Score 1-3.

REQUIRED CALIBRATION DISCIPLINE
You will be tempted to default to 8-9 on most fields because they "look
plausible." Resist this. Real legal contracts on real forms have genuinely
ambiguous fields — most upload runs SHOULD produce a non-trivial number of
sub-7 confidence scores. If your output has zero fields below 7, you have
miscalibrated and the downstream gate has not protected the user from
wrong-fills.

The penalty for wrong-fill on a $1M contract is far higher than the
penalty for blank-fill (which the user notices and corrects in 10
seconds). Score conservatively. When in doubt between 6 and 7, choose 6.
"""


# Concise text version of TransactionFields + the computed-context values
# interpolate.py injects at fill time. The computed values are critical:
# without them the AI classifies "Today's Date" / "Lease Sign Date" /
# "Agent Signed Date" / similar runtime-generated fields as template
# extras instead of mapping them to the {today} / {today_month_day} /
# {tenant_1_name} etc. paths that fill_pdf already produces. Every default
# IL mapping uses these — keep this list in sync with interpolate.build_context.
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

Computed values (auto-filled at generate time, use as canonical_path):
  today                       MM/DD/YYYY of fill date — for any field labeled
                               "Today's Date", "Date", "Sign Date", "Signed
                               Date", "Lease Date", "Sales Manager Signed Date",
                               or any blank that the agent fills with the
                               current date when generating the document.
  today_month_day             M/D for split-date layouts ("___, 20___")
  today_year_2digit           YY for split-date layouts (the "20___" half)
  property.address_full       full one-line "ADDR Unit U, City, ST ZIP"
  county_suffix               ", Cook County" or "" — appended after addresses
  tenant_1_name               first tenant in tenant_or_buyer_names list
  tenant_2_name               second tenant
  buyer_names_joined          all buyer names joined "A & B" or "A, B, C"
  seller_names_joined         all seller names joined the same way
  buyer_city_state_zip        one-line "City, ST ZIP" of buyer's residence
  closing_date_month_day      M/D split of closing_date
  closing_date_year_2digit    YY split of closing_date
  lease_end_month_day         M/D split of lease_end
  lease_end_year_2digit       YY split of lease_end
  additional_earnest_month_day, additional_earnest_year_2digit (same pattern)

When you see a date field, prefer a computed value over making it an
extra_field. "Today's Date" → canonical_path='today'. "Sign Date" → 'today'.
"Lease End Date" → 'lease_end'. Only use extra_field for dates that aren't
the fill date and aren't already a canonical schema field.
"""


# ---------- Public API ----------

def save_uploaded_pdf(pdf_bytes: bytes, template_id: str) -> Path:
    """Persist the uploaded PDF under templates/pdf/<id>.pdf. Returns the
    on-disk path so callers can record it on the Template row."""
    TEMPLATES_PDF_DIR.mkdir(parents=True, exist_ok=True)
    target = TEMPLATES_PDF_DIR / f"{template_id}.pdf"
    target.write_bytes(pdf_bytes)
    return target


def validate_pdf(pdf_bytes: bytes) -> tuple[PdfReader, bytes]:
    """Open + sanity-check the PDF. Returns (reader, bytes_to_persist).

    For normal AcroForm PDFs (Multi-Board, Compass Lease Abstract, etc.) the
    returned bytes are the original — zero new work. For flattened PDFs
    (CAR-via-iLovePDF, "saved as", etc.) we fall through to field_synth
    which renders pages and detects blank rectangles via OpenCV, then
    writes a real /AcroForm into the PDF. The returned bytes are the
    SYNTHESIZED version — callers MUST persist these (not the original)
    so the saved-on-disk PDF matches the in-memory field tree.

    Raises TemplateUploadError on:
      - malformed PDF (pypdf can't parse)
      - encrypted PDF (we don't unlock)
      - no AcroForm AND field synthesis found zero blanks (genuine scan)
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        raise TemplateUploadError(f"could not parse PDF: {e}")

    if reader.is_encrypted:
        raise TemplateUploadError(
            "this PDF is password-protected — unlock it before uploading"
        )

    fields = walk_fields(reader)
    if fields:
        # AcroForm path — the common case. Use existing widget metadata
        # directly; synth never runs for these uploads.
        return (reader, pdf_bytes)

    # Flattened-PDF path. Import lazily so the OpenCV/pdfminer deps are only
    # touched when actually needed (and the import error surface is one
    # caller, not every test that imports templates).
    from . import field_synth

    new_bytes, n_added = field_synth.try_synthesize(pdf_bytes)
    if n_added == 0:
        # Detection ran but found nothing fillable — genuine scan or
        # narrative document. User-facing message stays specific so they
        # know we tried.
        raise TemplateUploadError(
            "we couldn't find any fillable areas in this PDF. If it's a scan "
            "or photo, try re-exporting it as a digital PDF first."
        )
    print(f"field_synth: added {n_added} synthetic fields", flush=True)
    # Re-open the new bytes through a fresh PdfReader so downstream code sees
    # the synthesized fields. walk_fields cache for the original reader is
    # left in place (it's correctly empty) — the new reader is a different
    # object so the WeakKeyDictionary cache miss is automatic.
    new_reader = PdfReader(io.BytesIO(new_bytes))
    return (new_reader, new_bytes)


def collect_field_descriptions(reader: PdfReader) -> list[dict]:
    """For each AcroForm field, build the dict we hand the AI: name, type,
    neighbor text, and rect coordinates.

    Some fields (like Multi-Board's 'Address') have many widget instances
    that repeat in every page footer. The first widget in walk_fields order
    is whichever pypdf returned first, which is often a footer copy with
    useless 'Buyer Initial Seller Initial Address:' neighbor text. Pick
    the LARGEST widget instead — primary content fields are almost always
    much wider/taller than footer reprints.

    For /Btn and /Ch fields we ALSO emit the widget's /AP/N state names so
    the AI can pick the literal state per-PDF instead of guessing '/On'.
    Without this, every uploaded PDF whose checkboxes don't use exactly
    '/On' (revisions, vendor exports, scanned-then-re-AcroForm'd templates)
    fills blank because pdf_fill writes /Off when the proposed state isn't
    in the widget's /AP/N. Skipped on /Tx and /Sig — those don't have
    appearance states.
    """
    out: list[dict] = []
    for fi in walk_fields(reader):
        primary = _pick_primary_widget(fi.widgets)
        neighbor = ""
        if primary and primary.rect and primary.page > 0:
            neighbor = extract_neighbor_text(reader, primary.page, primary.rect)
        entry: dict = {
            "pdf_field": fi.dotted_name,
            "field_type": fi.field_type,
            "neighbor_text": neighbor[:300],
            "page": primary.page if primary else 0,
        }
        if fi.field_type in ("/Btn", "/Ch"):
            states = fi.states
            if states:
                entry["states"] = states
        out.append(entry)
    return out


def _pick_primary_widget(widgets):
    """Pick the most informative widget for label extraction. Heuristic:
    largest rect area (primary content fields are usually full-width form
    blanks; footer reprints are narrower or shorter). Falls back to the
    first widget if rects are missing."""
    candidates = [w for w in widgets if w.rect and w.page > 0]
    if not candidates:
        return widgets[0] if widgets else None

    def area(w):
        if not w.rect:
            return 0.0
        x0, y0, x1, y1 = w.rect
        return abs(x1 - x0) * abs(y1 - y0)

    return max(candidates, key=area)


# When a form has more than this many fields, chunk the propose_mapping call
# into batches. The system prompt + schema hint carry fixed cost; field
# descriptors + crops are variable. Chunking keeps each call under ~30k tokens
# and avoids hitting context-window limits or response-truncation issues on
# the 389-field Multi-Board contract.
CHUNK_SIZE = 120


async def _propose_mapping_chunk(
    client: OpenAI,
    chunk: list[dict],
    crops: dict[str, str] | None,
) -> ProposedMapping:
    """Send one batch of field descriptors (with optional visual crops) to
    GPT and parse the result. Used by propose_mapping for both single-shot
    and chunked calls.

    reasoning_effort="low" because this is a classification task — given a
    field name + neighbor text + (optional) crop, pick a canonical_path from
    a known allowlist. We already validate the AI's output post-hoc via
    _build_canonical_path_allowlist, so the model's "high reasoning effort"
    default (~25-50s/chunk overhead) buys us no quality and costs real time.
    Verified: classification accuracy is comparable on this task at "low".
    """
    # Build the user message. Start with the schema hint + the JSON-encoded
    # field list, then attach a cropped image for each field whose
    # neighbor_text is empty (and where we successfully rendered a crop).
    user_text = (
        f"{CANONICAL_SCHEMA_HINT}\n\n"
        f"Map every field below. Score confidence honestly. "
        f"Return a ProposedMapping.\n\n"
        f"FIELDS:\n{json.dumps(chunk, indent=2)}"
    )

    user_content: list[dict] = [{"type": "input_text", "text": user_text}]

    if crops:
        # Attach crops only for fields in this chunk. Each crop arrives as
        # a labeled image block; the AI cross-references via the explicit
        # "FIELD '<name>' CROP:" prefix that we put just before each image.
        for fd in chunk:
            name = fd.get("pdf_field")
            if not name or name not in crops:
                continue
            user_content.append({
                "type": "input_text",
                "text": f"\nFIELD {name!r} CROP (visual context for the area around this field):",
            })
            user_content.append({
                "type": "input_image",
                "image_url": f"data:image/png;base64,{crops[name]}",
            })

    try:
        response = await asyncio.to_thread(
            client.responses.parse,
            model=MODEL,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            text_format=ProposedMapping,
            reasoning={"effort": "low"},
        )
    except Exception as e:
        raise AIMappingError(f"OpenAI API call failed: {e}")

    parsed = response.output_parsed
    if parsed is None:
        raise AIMappingError("OpenAI returned no parsed output")
    return parsed


async def propose_mapping(
    field_descriptions: list[dict],
    crops: dict[str, str] | None = None,
) -> ProposedMapping:
    """Ask GPT-5 to propose a mapping for every field in field_descriptions.

    crops: optional dict mapping pdf_field name -> base64 PNG of the field's
    surrounding area. Used to give the AI visual context for fields where
    text extraction missed the label. See pdf_render.collect_field_crops.

    For forms with more than CHUNK_SIZE fields, splits into batches and runs
    them concurrently via asyncio.gather (was sequential — that was a stale
    rate-limit precaution from when gpt-5 Tier 1 was 30K TPM. Tier 1 is now
    500K TPM; 4 parallel chunks at ~4K tokens each = 3% of limit, safe).
    The order of fields is preserved by gather's positional return.

    Partial-success handling: if a single chunk raises (timeout, transient
    OpenAI 5xx), we keep the successful chunks and surface a warning rather
    than discarding everything. Users get a partially-mapped template they
    can review and complete manually instead of a 502 dead-end.

    Raises AIMappingError only when ALL chunks fail — partial-failure is
    still considered a usable result.
    """
    client = _get_client()

    if len(field_descriptions) <= CHUNK_SIZE:
        return await _propose_mapping_chunk(client, field_descriptions, crops)

    # Slice into chunks. gather preserves positional order in its return,
    # so concatenating in-order rebuilds the original field sequence.
    chunks = [
        field_descriptions[i : i + CHUNK_SIZE]
        for i in range(0, len(field_descriptions), CHUNK_SIZE)
    ]

    # Concurrency cap: gpt-5 Tier 1 = 500K TPM and 4 parallel chunks is safe,
    # but a buggy 50-chunk upload or two overlapping uploads would otherwise
    # fire arbitrary concurrent OpenAI calls. Semaphore(4) bounds it.
    sem = asyncio.Semaphore(4)

    async def _bounded(chunk: list[dict]) -> ProposedMapping:
        async with sem:
            return await _propose_mapping_chunk(client, chunk, crops)

    results = await asyncio.gather(
        *(_bounded(chunk) for chunk in chunks),
        return_exceptions=True,
    )

    all_fields: list[ProposedField] = []
    failures: list[Exception] = []
    successful_chunks = 0
    for chunk_idx, result in enumerate(results):
        if isinstance(result, Exception):
            failures.append(result)
            # Synthesize unmapped placeholders for this chunk's fields so
            # the user still sees them in the review UI as low-confidence
            # candidates they can map by hand. Without this, fields from the
            # failed chunk vanish entirely from the proposed mapping.
            for fd in chunks[chunk_idx]:
                pdf_field = fd.get("pdf_field") or ""
                if not pdf_field:
                    continue
                all_fields.append(
                    ProposedField(
                        pdf_field=pdf_field,
                        canonical_path=None,
                        template_value=None,
                        confidence=0,
                        reasoning=f"AI mapping chunk failed: {type(result).__name__}",
                    )
                )
            continue
        successful_chunks += 1
        all_fields.extend(result.fields)

    if successful_chunks == 0:
        # Every chunk failed. Even though we have placeholder fields, an
        # all-unmapped template is worse than an error — the user gets a
        # form with zero suggestions and no signal anything went wrong.
        # Better to 502 and let them retry the whole upload.
        raise AIMappingError(
            f"all {len(failures)} mapping chunks failed; first error: {failures[0]}"
        )

    return ProposedMapping(fields=all_fields)


# Allowlist of canonical paths the AI is allowed to propose. Derived from
# TransactionFields + AgentProfile + the computed-value keys interpolate.py
# emits at fill time. Anything outside this set is a hallucination — at fill
# time it would silently render empty (the interpolate regex matches but
# _resolve returns None), giving users a blank PDF with no signal as to why.
# Surfacing it at upload-review time costs nothing and saves the surprise.
def _build_canonical_path_allowlist() -> tuple[set[str], dict[str, frozenset[str]]]:
    """Returns (path_allowlist, literal_values_by_path).

    path_allowlist: all canonical paths the AI may propose.
    literal_values_by_path: for paths whose TransactionFields type is a Literal
        enum (e.g. property_type, escrowee, loan_type), the allowed string
        values. Used to validate btn_choices keys at proposal-to-mapping time:
        AI emitting {'multifamily': '/On'} for property_type (Literal[attached,
        detached, multi_unit]) gets dropped.
    """
    from typing import get_args, get_origin, Literal
    from .schema import AgentProfile, Property, TransactionFields  # local import to avoid cycle

    allowed: set[str] = set()
    literals: dict[str, frozenset[str]] = {}

    def collect_literal(path: str, annotation: object) -> None:
        """If annotation is Literal[...] or Literal[...] | None, capture its
        string values under `path`. Handles the X|None unions Pydantic emits.

        Detection order matters: check Literal FIRST because typing.Literal
        is itself the origin (get_origin returns typing.Literal), and recursing
        into its args (which are plain strings, not types) would loop on
        non-type values."""
        origin = get_origin(annotation)
        # Literal[...] direct hit.
        if origin is Literal:
            values = {str(v) for v in get_args(annotation)}
            if values:
                literals[path] = frozenset(values)
            return
        # Union / X | None — recurse on each non-None branch.
        if origin is not None:
            for arg in get_args(annotation):
                if arg is type(None):
                    continue
                collect_literal(path, arg)

    # TransactionFields scalar + nested keys
    for name, field in TransactionFields.model_fields.items():
        if name == "property":
            for prop_name in Property.model_fields:
                allowed.add(f"property.{prop_name}")
        else:
            allowed.add(name)
            collect_literal(name, field.annotation)

    # AgentProfile nested under "agent."
    for name in AgentProfile.model_fields:
        allowed.add(f"agent.{name}")

    # Computed values emitted by interpolate.build_context. These have to be
    # kept in sync with that function — see CANONICAL_SCHEMA_HINT for the same
    # list documented for the AI.
    allowed.update({
        "today",
        "today_month_day",
        "today_year_2digit",
        "property.address_full",
        "county_suffix",
        "tenant_1_name",
        "tenant_2_name",
        "buyer_names_joined",
        "seller_names_joined",
        "buyer_city_state_zip",
        "closing_date_month_day",
        "closing_date_year_2digit",
        "lease_end_month_day",
        "lease_end_year_2digit",
        "additional_earnest_month_day",
        "additional_earnest_year_2digit",
        # AcroForm checkbox/radio state names from interpolate (bundled-mapping path)
        "property_type_attached_state",
        "property_type_detached_state",
        "property_type_multi_unit_state",
        "escrowee_state",
        "seller_pays_brokerage_state",
        "commission_percent_value",
        "commission_dollar_value",
        "loan_rate_type_state",
        "loan_type_state",
        "statutory_state",
    })
    return allowed, literals


_CANONICAL_PATH_ALLOWLIST, _CANONICAL_LITERAL_VALUES = _build_canonical_path_allowlist()


# Mappings with confidence < this threshold get blanked at fill time and
# surfaced to the user as "uncertain fields" rather than being filled with
# possibly-wrong data. Reasoning (incident 2026-05-10): a single wrong field
# in a sale contract is a business-ending event; a blank field is recoverable
# with 30 seconds of user input.
LOW_CONFIDENCE_THRESHOLD = 7


def _sanitize_btn_choices(
    btn_choices: dict[str, str],
    canonical_path: str,
    widget_states: list[str],
) -> tuple[dict[str, str], list[str]]:
    """Filter btn_choices to only entries the system can fill correctly.

    Drops:
      - keys not in the canonical Literal enum (e.g. AI emits 'multifamily'
        for property_type whose values are attached|detached|multi_unit)
      - state values not in the widget's actual /AP/N keys (e.g. AI emits
        '/On' when the widget only accepts '/Yes')

    Returns (cleaned_choices, warnings). Warnings are human-readable strings
    intended for validate_mapping_structure / needs_attention flagging.

    Why per-key filtering instead of all-or-nothing reject: a radio with 3
    options where the AI gets 2 right and 1 wrong is more useful than
    blanking the whole field. The wrong 1 just doesn't fire — which is the
    same correct-by-default behavior as blank.
    """
    if not btn_choices:
        return {}, []
    cleaned: dict[str, str] = {}
    warnings: list[str] = []
    allowed_values = _CANONICAL_LITERAL_VALUES.get(canonical_path)
    # Empty list is meaningful: the field has /AP/N but no states extracted, OR
    # we couldn't find the field in field_descriptions. Either way we can't
    # trust the AI's state proposal — reject everything to avoid silently
    # accepting hallucinated states. None means "no widget-state context
    # available" (test path with field_descriptions=None) — skip the check.
    widget_state_set = set(widget_states) if widget_states is not None else None
    for value, state in btn_choices.items():
        if allowed_values is not None and value not in allowed_values:
            warnings.append(
                f"btn_choices for path '{canonical_path}' has unknown value '{value}' "
                f"(allowed: {sorted(allowed_values)})"
            )
            continue
        if widget_state_set is not None and state not in widget_state_set:
            warnings.append(
                f"btn_choices for path '{canonical_path}' proposes state '{state}' "
                f"not in widget /AP/N (allowed: {sorted(widget_state_set)})"
            )
            continue
        cleaned[value] = state

    # Silent-no-op detection: if every cleaned entry maps to "/Off" the field
    # will never fire its checkbox regardless of ctx — same observable result
    # as having NO mapping at all, but without the low_confidence surface.
    # Reject the whole table so the caller blanks + surfaces it instead of
    # writing a useless BtnChoice. Real cause: AI gets the canonical values
    # right but the on-state wrong, OR is genuinely confused about which kid
    # is the "on" widget. Caught by adversarial review on 2026-05-10.
    if cleaned and all(state == "/Off" for state in cleaned.values()):
        warnings.append(
            f"btn_choices for path '{canonical_path}' maps every canonical "
            f"value to '/Off' — checkbox would never fire. Rejected."
        )
        return {}, warnings

    return cleaned, warnings


def proposal_to_mapping_file(
    proposal: ProposedMapping,
    title: str,
    source_pdf_filename: str,
    filled_filename: str,
    field_descriptions: list[dict] | None = None,
) -> tuple[MappingFile, list[ExtraField], list[str], list[dict], list[str]]:
    """Convert the AI's ProposedMapping into the on-disk MappingFile shape +
    extras + a list of low-confidence fields.

    Returns (mapping, extras, unknown_paths, low_confidence_fields, btn_warnings).
      - mapping: MappingFile written to disk; fill_pdf reads this.
      - extras: ExtraField list for templates.extra_fields column.
      - unknown_paths: AI-proposed canonical paths that don't exist in our
        schema. Demoted to unmapped-empty.
      - low_confidence_fields: list of {pdf_field, canonical_path,
        extra_field_name, confidence, reason} — fields where the AI's
        confidence is below threshold. Written into the mapping JSON's
        `low_confidence` block so /api/generate can surface them to the
        user without re-running the AI.
      - btn_warnings: list of human-readable warnings about /Btn proposals
        that had btn_choices values pruned for being invalid (unknown
        canonical-enum value or unknown widget state). Fed into
        validate_mapping_structure so the template can be flagged
        needs_attention without blocking upload.

    `field_descriptions` is optional; when provided (the production
    upload path always provides it) we use it to validate btn_choices
    state names against each widget's actual /AP/N keys. Without it
    (some test paths) we skip the widget-state check but still validate
    against the canonical Literal enum.

    Confidence-gating policy: fields below threshold get blanked in the
    mapping (renders empty at fill time) AND surfaced to the user as
    "we weren't sure — fill these in by hand". Wrong > blank on a legal
    contract, so we err toward blank.
    """
    fields: dict[str, str | BtnChoice] = {}
    extras: list[ExtraField] = []
    unknown_paths: list[str] = []
    low_confidence_fields: list[dict] = []
    btn_warnings: list[str] = []

    # Build pdf_field → (widget states, field_type) lookup once. Empty list when the
    # field has no /AP/N (i.e. /Tx or /Sig — btn_choices shouldn't be there anyway).
    # field_type lookup is used to GATE btn_choices: AI may hallucinate btn_choices
    # on a /Tx field via prompt injection or confusion, and routing those through
    # BtnChoice silently corrupts the text field's /V at fill time (renders "/Off"
    # into an address line). Defense-in-depth: only honor btn_choices when the
    # field is actually a /Btn or /Ch. Caught by code review on 2026-05-10.
    widget_states_by_field: dict[str, list[str]] = {}
    field_type_by_field: dict[str, str] = {}
    if field_descriptions is not None:
        for fd in field_descriptions:
            name = fd.get("pdf_field")
            if name is not None:
                widget_states_by_field[name] = list(fd.get("states") or [])
                field_type_by_field[name] = str(fd.get("field_type") or "")

    for f in proposal.fields:
        is_low_confidence = f.confidence < LOW_CONFIDENCE_THRESHOLD

        if f.canonical_path:
            if f.canonical_path not in _CANONICAL_PATH_ALLOWLIST:
                # AI hallucinated a path. Don't write the broken reference;
                # leave the field unmapped so the human sees a blank rather
                # than a silent-render-empty.
                fields[f.pdf_field] = ""
                unknown_paths.append(f"{f.pdf_field} → {f.canonical_path}")
            elif is_low_confidence:
                # Plausible path but AI isn't confident. Blank it and surface.
                fields[f.pdf_field] = ""
                low_confidence_fields.append({
                    "pdf_field": f.pdf_field,
                    "proposed": f.canonical_path,
                    "confidence": f.confidence,
                    "kind": "canonical",
                })
            elif f.btn_choices and field_type_by_field.get(f.pdf_field) == "/Btn":
                # /Btn field with a conditional state table. Sanitize keys
                # (must match canonical Literal enum) and values (must match
                # widget /AP/N states). Surviving entries become a BtnChoice.
                # GATING (added 2026-05-10 after code review): we only enter this
                # branch when the field is actually a /Btn — otherwise routing
                # btn_choices through BtnChoice would silently corrupt:
                #   - /Tx fields: write "/Off" into a text blank
                #   - /Ch fields: write a state name into a dropdown /V
                # /Ch fields fall through to the string-template path below,
                # which is correct (dropdowns take string values, not states).
                widget_states = widget_states_by_field.get(f.pdf_field, [])
                cleaned, warns = _sanitize_btn_choices(
                    f.btn_choices, f.canonical_path, widget_states
                )
                if warns:
                    btn_warnings.extend(
                        f"{f.pdf_field}: {w}" for w in warns
                    )
                if cleaned:
                    fields[f.pdf_field] = BtnChoice(
                        canonical_path=f.canonical_path,
                        choices=cleaned,
                    )
                else:
                    # All choices pruned — nothing usable. Blank + surface.
                    fields[f.pdf_field] = ""
                    low_confidence_fields.append({
                        "pdf_field": f.pdf_field,
                        "proposed": f.canonical_path,
                        "confidence": f.confidence,
                        "kind": "canonical",
                    })
            elif field_type_by_field.get(f.pdf_field) == "/Btn":
                # /Btn field WITHOUT btn_choices. The AI didn't tell us which
                # state to fire, and a string template like "{property_type}"
                # would interpolate to a raw enum string ("attached") that
                # pdf_fill writes as text into the field's /V — leaving the
                # checkbox visually unchecked. Treat as low-confidence: blank
                # the field + surface so the user knows to fill it manually.
                fields[f.pdf_field] = ""
                low_confidence_fields.append({
                    "pdf_field": f.pdf_field,
                    "proposed": f.canonical_path,
                    "confidence": f.confidence,
                    "kind": "canonical",
                })
                btn_warnings.append(
                    f"{f.pdf_field}: /Btn field mapped to '{f.canonical_path}' "
                    f"without btn_choices — blanked (AI would have written raw "
                    f"value to /V instead of selecting a checkbox state)"
                )
            else:
                fields[f.pdf_field] = "{" + f.canonical_path + "}"
        elif f.extra_field_name:
            if is_low_confidence:
                # Don't even register the extra — surface and blank.
                fields[f.pdf_field] = ""
                low_confidence_fields.append({
                    "pdf_field": f.pdf_field,
                    "proposed": f.extra_field_name,
                    "confidence": f.confidence,
                    "kind": "extra",
                })
            elif f.btn_choices and field_type_by_field.get(f.pdf_field) == "/Btn":
                # /Btn field tied to a template_extras boolean (e.g. dual_agency
                # checkbox). Same sanitization, path is template_extras.<name>.
                # GATING: only honor btn_choices on actual /Btn or /Ch fields —
                # see canonical_path branch above for the same defense.
                synthetic_path = f"template_extras.{f.extra_field_name}"
                widget_states = widget_states_by_field.get(f.pdf_field, [])
                # For extras we don't know the Literal enum (it's user-defined),
                # so only validate state names against widget /AP/N.
                cleaned, warns = _sanitize_btn_choices(
                    f.btn_choices, synthetic_path, widget_states
                )
                if warns:
                    btn_warnings.extend(
                        f"{f.pdf_field}: {w}" for w in warns
                    )
                if cleaned:
                    fields[f.pdf_field] = BtnChoice(
                        canonical_path=synthetic_path,
                        choices=cleaned,
                    )
                    extras.append(ExtraField(
                        name=f.extra_field_name,
                        type=f.extra_field_type or "bool",
                        description=f.extra_field_description or "",
                        pdf_field=f.pdf_field,
                    ))
                else:
                    fields[f.pdf_field] = ""
                    low_confidence_fields.append({
                        "pdf_field": f.pdf_field,
                        "proposed": f.extra_field_name,
                        "confidence": f.confidence,
                        "kind": "extra",
                    })
            else:
                # Reference into template_extras.
                fields[f.pdf_field] = "{template_extras." + f.extra_field_name + "}"
                extras.append(ExtraField(
                    name=f.extra_field_name,
                    type=f.extra_field_type or "text",
                    description=f.extra_field_description or "",
                    pdf_field=f.pdf_field,
                ))
        else:
            # AI returned neither — treat as unmapped (empty string).
            fields[f.pdf_field] = ""

    mapping = MappingFile(
        meta=MappingMeta(
            title=title,
            source_pdf=source_pdf_filename,
            filled_filename=filled_filename,
        ),
        fields=fields,
        low_confidence=low_confidence_fields,
    )
    return mapping, extras, unknown_paths, low_confidence_fields, btn_warnings


def _mapping_value_is_blank(v: str | BtnChoice) -> bool:
    """A mapping value is 'blank' (will fill nothing at runtime) when it's
    an empty/whitespace string, OR a BtnChoice whose choices either:
      - empty: sanitizer rejected every entry, hand-edited JSON
      - all '/Off': every canonical value produces /Off → widget never fires

    Both BtnChoice cases are equivalent to "" at fill time but pre-fix were
    counted as non-blank in coverage_low. Now they correctly count toward
    the blank ratio so coverage warnings fire when they should."""
    if isinstance(v, str):
        return not v.strip()
    if not v.choices:
        return True
    # All-/Off table: sanitizer normally rejects these but hand-edited
    # mapping JSON can sneak one through. Treat as blank for coverage stats.
    return all(state == "/Off" for state in v.choices.values())


def validate_mapping_structure(
    mapping: MappingFile,
    field_descriptions: list[dict],
    unknown_paths: list[str],
    low_confidence_fields: list[dict],
    btn_warnings: list[str] | None = None,
) -> list[str]:
    """Sanity-check the mapping after proposal_to_mapping_file. Returns a
    list of warnings. Empty list = clean mapping ready to ship.

    Catches the worst mapping failures cheaply, with no API calls. Used by
    main.py to decide whether to mark the template `ready` or
    `needs_attention` at upload time.

    The denominator for coverage / hallucination / low-confidence checks is
    `len(mapping.fields)` — how many proposals the AI actually returned.
    Not `len(field_descriptions)`, which would count fields the AI didn't
    even see. That distinction matters: "AI dropped 90% of fields" is a
    different failure (AI quality / batching bug) from "AI mapped fields
    but mostly to nothing usable."

    Checks:
    1. Dropped fields — AI returned fewer proposals than fields exist.
       Flag if dropped >5% (the AI was supposed to map every field).
    2. Coverage — of the fields the AI did return, what fraction ended up
       as a blank mapping (no canonical, no extra, or low-conf-blanked)?
       Flag if >50%.
    3. Hallucinated paths — flag if >5%.
    4. Low-confidence concentration — flag if >30%.
    5. /Btn proposal sanitization warnings — if proposal_to_mapping_file
       had to drop btn_choices entries, surface them so the human reviewer
       knows the AI got widget states or canonical enums wrong.
    """
    warnings: list[str] = []
    total = len(field_descriptions)
    proposed = len(mapping.fields)
    if total == 0:
        return warnings

    dropped = total - proposed
    if dropped > max(1, int(0.05 * total)):
        warnings.append(
            f"dropped_fields: AI returned proposals for only {proposed}/{total} "
            f"fields. {dropped} fields are unmapped because the AI didn't return them."
        )

    if proposed > 0:
        # Per F4/F10 from outside-voice review: mapping.fields is now
        # dict[str, str | BtnChoice]. .strip() crashes on BtnChoice. Route
        # through _mapping_value_is_blank to handle both shapes.
        blank_count = sum(1 for v in mapping.fields.values() if _mapping_value_is_blank(v))
        blank_pct = blank_count / proposed
        if blank_pct > 0.5:
            warnings.append(
                f"coverage_low: {blank_pct:.0%} of proposed mappings are blank "
                f"({blank_count}/{proposed}). AI signal was insufficient."
            )

        hallucination_pct = len(unknown_paths) / proposed
        if hallucination_pct > 0.05:
            warnings.append(
                f"hallucinated_paths: {len(unknown_paths)} fields ({hallucination_pct:.0%}) "
                f"map to canonical paths that don't exist."
            )

        low_conf_pct = len(low_confidence_fields) / proposed
        if low_conf_pct > 0.30:
            warnings.append(
                f"low_confidence_high: {len(low_confidence_fields)} fields "
                f"({low_conf_pct:.0%}) are below confidence threshold. User will "
                f"need to fill many fields by hand."
            )

    if btn_warnings:
        # /Btn-specific warnings: AI emitted choices that didn't match the
        # widget's actual /AP/N keys or the canonical Literal enum. Flag the
        # template so the agent reviews it before generating. Cap at 5 in the
        # surfaced text so the warning column stays readable; the full list
        # lives in the mapping's audit trail.
        sample = btn_warnings[:5]
        more = f" (+{len(btn_warnings) - 5} more)" if len(btn_warnings) > 5 else ""
        warnings.append(
            f"btn_choice_mismatches: {len(btn_warnings)} /Btn proposal(s) had "
            f"invalid choices pruned. Examples: {sample}{more}"
        )

    return warnings


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
    pdf_sha256: str | None = None,
    status: TemplateStatus = "pending_review",
) -> Template:
    """Construct a Template row. Paths are stored relative to repo root for
    portability when inside the repo, absolute otherwise. user_id is required
    (no default) so a forgotten parameter doesn't silently leak ownership.

    pdf_sha256 is the cache key for the AI mapping — callers should pass it
    so a re-upload of the same PDF can short-circuit the mapping call. It's
    nullable for back-compat with tests and call sites that pre-date the
    pdf_sha256 column."""
    if not user_id:
        raise ValueError("user_id is required when building a template row")
    return Template(
        id=template_id,
        user_id=user_id,
        title=title,
        source_pdf_path=_path_for_storage(source_pdf_path),
        mapping_path=_path_for_storage(mapping_path),
        status=status,
        is_default=False,
        extra_fields=extras,
        pdf_sha256=pdf_sha256,
        created_at=now_iso(),
    )
