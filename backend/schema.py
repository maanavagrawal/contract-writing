"""
Canonical field schema for a transaction.

One source of truth used by:
  - the OpenAI structured-output call (TransactionFields → JSON schema)
  - the mapping interpolator (paths like {property.address})
  - the frontend form (every field name matches a form input)

All fields are nullable so extraction is always best-effort. Money is a string
to preserve the agent's exact formatting ("$3,182" not 3182.0). Dates are ISO
strings (YYYY-MM-DD) so they round-trip cleanly through JSON without timezone
ambiguity.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Property(BaseModel):
    address: str | None = Field(None, description="Street address only, no unit number. e.g. '221 W Hubbard'")
    unit: str | None = Field(None, description="Unit / apartment number if present. e.g. '803'")
    city: str | None = None
    state: str | None = Field(None, description="Two-letter state code. e.g. 'IL'")
    zip: str | None = None


class TransactionFields(BaseModel):
    """Everything we try to extract from the agent's notes + MLS screenshots."""

    transaction_type: Literal["lease", "sale"] | None = Field(
        None,
        description=(
            "'lease' if the notes mention monthly rent, tenant, lease dates, or co-op. "
            "'sale' if they mention purchase price, closing date, buyer/seller. "
            "Null if ambiguous."
        ),
    )

    property: Property = Field(default_factory=Property)

    # lease-specific
    lease_start: str | None = Field(None, description="ISO date YYYY-MM-DD")
    lease_end: str | None = Field(None, description="ISO date YYYY-MM-DD")
    monthly_rent: str | None = Field(None, description="As written by the agent, e.g. '$3,182'")

    # sale-specific
    purchase_price: str | None = Field(None, description="As written, e.g. '$725,000'")
    closing_date: str | None = Field(None, description="ISO date YYYY-MM-DD")
    earnest_money: str | None = None
    earnest_business_days: str | None = Field(
        None, description="Business days after acceptance to tender earnest. Default 5 if not given."
    )
    additional_earnest_money: str | None = None
    additional_earnest_date: str | None = Field(None, description="ISO date YYYY-MM-DD")
    credit_at_closing: str | None = None

    # Sale-only structural fields
    seller_names: list[str] = Field(
        default_factory=list,
        description="Seller name(s). Empty for leases. One entry per person.",
    )
    property_type: Literal["attached", "detached", "multi_unit"] | None = Field(
        None, description="Single Family Attached / Single Family Detached / Multi-Unit (sale only)."
    )
    county: str | None = Field(None, description="Cook, DuPage, etc.")

    # Buyer's current residence (for the contract's buyer-contact block)
    buyer_address: str | None = None
    buyer_city: str | None = None
    buyer_state: str | None = None
    buyer_zip: str | None = None

    # Financing terms (Multi-Board section 8)
    loan_type: Literal["conventional", "fha", "va", "usda", "other"] | None = Field(
        None, description="Loan type. Default 'conventional' if not specified."
    )
    loan_type_other: str | None = Field(None, description="Free-text if loan_type is 'other'.")
    loan_rate_type: Literal["fixed", "adjustable"] | None = Field(
        None, description="Default 'fixed'."
    )
    loan_percent_of_price: str | None = Field(
        None, description="Loan amount as % of purchase price. e.g. '80' for 80%. Default 80."
    )
    loan_max_rate: str | None = Field(None, description="Max interest rate %, e.g. '7.5'.")
    loan_amortization_years: str | None = Field(None, description="Default '30'.")
    loan_max_points: str | None = Field(None, description="Default '1'.")

    # Tax/HOA proration
    tax_proration_percent: str | None = Field(
        None, description="% for tax proration. Default '110' for Cook County, '105' elsewhere."
    )
    hoa_fee: str | None = None
    hoa_frequency: Literal["month", "quarter", "year"] | None = None

    # Escrowee selection
    escrowee: Literal["seller", "buyer", "other"] | None = Field(
        None, description="Who holds earnest money. Default 'seller' if not specified."
    )

    # parties (tenant for leases, buyer for sales — same field, context decides)
    tenant_or_buyer_names: list[str] = Field(
        default_factory=list,
        description="One entry per person. Empty if not in notes.",
    )
    tenant_or_buyer_email: str | None = None
    tenant_or_buyer_phone: str | None = None

    # landlord side — for the invoice + Tenant Rep agreement
    landlord_billing_email: str | None = Field(
        None,
        description="The email the lease invoice should be sent to.",
    )

    # compensation
    commission_amount: str | None = Field(
        None,
        description="The dollar amount or percent the agent earns. As written.",
    )
    retainer: str | None = None

    # lease-specific extras (Lease Abstract, Tenant Rep)
    concessions: str | None = Field(
        None,
        description="Concessions like '1 month free' or 'no security deposit'. As written.",
    )
    net_monthly_rent: str | None = Field(
        None,
        description="Net rent if different from monthly_rent (e.g. after concessions). Otherwise null.",
    )
    protection_period_days: str | None = Field(
        None,
        description="Days for the post-term protection period in the Tenant Rep agreement. Default 30 if not mentioned.",
    )
    early_termination_fee: str | None = Field(
        None,
        description="Fee if Client terminates rep agreement early. Default $0 if not mentioned.",
    )


class AgentProfile(BaseModel):
    """Agent's own info — saved once on the frontend, sent with every generate."""

    name: str | None = None
    license: str | None = None
    brokerage: str = "Compass Illinois, Inc."
    brokerage_address: str | None = None
    brokerage_mls: str | None = Field(None, description="Brokerage's MLS office #.")
    brokerage_license: str | None = Field(None, description="Brokerage's IL state license #.")
    phone: str | None = None
    email: str | None = None
    mls: str | None = None


class GenerateRequest(BaseModel):
    fields: TransactionFields
    agent: AgentProfile
    documents: list[str] = Field(
        ...,
        description="List of document keys to generate, e.g. ['lease_invoice', 'lease_abstract']",
    )
    # Per-template extras values extracted from the agent's notes. Shape mirrors
    # what extract.py emits on the dynamic schema's TransactionFieldsExtended:
    # {template_id: {extra_field_name: value, ...}, ...}. The generate path
    # flattens these into ctx["template_extras"] so mapping strings like
    # "{template_extras.brbc_compensation_percent}" resolve at fill time. Without
    # this, every template-specific extras reference renders blank — the CAR
    # BRBC fill bug 2026-05-12 traced back to this missing argument.
    template_extras: dict[str, dict[str, object]] | None = None


class UncertainField(BaseModel):
    """One field that the AI mapped but with confidence below the gating
    threshold (see backend/templates.LOW_CONFIDENCE_THRESHOLD). These render
    BLANK in the PDF (better than wrong) and are surfaced to the user so
    they can fill them by hand. The shape mirrors mapping.low_confidence
    entries written at upload time."""
    pdf_field: str
    proposed: str               # canonical path or extra_field name the AI tried
    confidence: int             # 1-10
    kind: str                   # "canonical" | "extra"


class GeneratedDoc(BaseModel):
    document: str
    filename: str
    base64: str
    content_type: str = "application/pdf"
    # Fields the template's mapping marked low-confidence at upload time —
    # rendered blank in this filled PDF. Frontend surfaces them as
    # "we weren't sure, please fill these in" after generate.
    uncertain_fields: list[UncertainField] = Field(default_factory=list)


class GeneratedDocFailure(BaseModel):
    """One document that didn't make it through /api/generate. Lets the client
    show partial-success UI instead of 500ing the whole batch."""
    document: str
    error: str


class GenerateResponse(BaseModel):
    documents: list[GeneratedDoc]
    failures: list[GeneratedDocFailure] = Field(default_factory=list)


# ---- Mapping JSON validation ----

class MappingMeta(BaseModel):
    """The _meta block at the top of every backend/mappings/*.json."""
    title: str
    source_pdf: str
    filled_filename: str
    notes: str | None = None


class BtnChoice(BaseModel):
    """A conditional state-name lookup for an AcroForm /Btn field (checkbox
    or radio group). At fill time the resolver reads ctx[canonical_path],
    looks up the value in `choices`, and emits the matching state name —
    e.g. ctx['property_type']='attached' + choices={'attached':'/On',
    'detached':'/Off'} → '/On' written to the widget's /AS.

    Why a table instead of a single (when_value, on_state) pair: real radio
    groups have ONE pdf_field with N widget kids that each accept a different
    /AP/N state. A single pair can only encode one (canonical_value → state)
    mapping. The choices dict encodes the full N-way dispatch in one entry.
    fill_pdf already routes each widget kid to /On or /Off based on whether
    the resolved state matches its /AP/N — so we just need to produce the
    right ONE state string per field.

    When ctx[canonical_path] is None, missing, or not a key in `choices`,
    the resolver returns '/Off' — never falsely fires a checkbox.
    """
    canonical_path: str = Field(description="Dotted ctx path. e.g. 'property_type' or 'template_extras.dual_agency'.")
    choices: dict[str, str] = Field(description="Map from canonical value (str) to widget /AP/N state name (e.g. '/On' or '/Choice1').")


class MappingFile(BaseModel):
    """Pydantic-validated shape of a mapping JSON. Loaded by generate.py and
    by the upcoming template-upload flow (which validates user-uploaded
    mappings before writing them to disk).

    `meta` is read from the JSON's "_meta" key; the `alias` lets us keep the
    file format unchanged while exposing a Python-friendly attribute name.

    `low_confidence` is the post-upload list of fields the AI mapped with
    confidence < threshold. Those fields render BLANK in generated PDFs and
    the frontend surfaces them as "we weren't sure, fill these in by hand."
    Per-field shape: {pdf_field, proposed, confidence, kind}.

    `fields` is a dict from pdf field name to either a string template
    ("{property.address}") or a BtnChoice (conditional state lookup for
    /Btn fields). Both shapes coexist in the same dict; the resolver
    dispatches per type. Bundled canonical mappings on disk are all
    string-only and continue to work unchanged.
    """
    meta: MappingMeta = Field(alias="_meta")
    fields: dict[str, str | BtnChoice]
    extra_fields: list[dict] = Field(default_factory=list)
    low_confidence: list[dict] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ---- Edit-in-preview ----

class FieldOverlayDTO(BaseModel):
    """One AcroForm field with everything the frontend needs to render an
    editable overlay on top of the rendered PDF page image."""
    name: str
    field_type: str                # "/Tx" | "/Btn" | "/Ch" | "/Sig"
    page: int                      # 1-based
    rect_px: tuple[float, float, float, float]  # x, y, width, height in PNG pixels
    value: str
    # /Btn appearance state names (e.g. ["/Off", "/On"] for a simple checkbox,
    # ["/Off", "/Choice1", "/Choice2", ...] for a radio group). The frontend
    # uses this to detect radios so it can render them as read-only instead
    # of a checkbox that would clobber the actual selection on round-trip.
    states: list[str] = []


class PageRenderDTO(BaseModel):
    page: int
    width_px: int
    height_px: int
    image_b64: str
    content_type: str = "image/png"


class PreviewRequest(BaseModel):
    base64_pdf: str


class PreviewResponse(BaseModel):
    pages: list[PageRenderDTO]
    fields: list[FieldOverlayDTO]


class EditRequest(BaseModel):
    """Apply user edits to a previously-generated PDF. The frontend sends back
    only the dotted names + new values; we re-fill on top of the source PDF."""
    base64_pdf: str
    edits: dict[str, str]


class EditResponse(BaseModel):
    """Round-trip the edited PDF as base64 + a fresh preview render so the UI
    can update the page images and any field defaults that depend on each other."""
    document: GeneratedDoc
    preview: PreviewResponse


# ---- Template management (Pillar 2) ----

class TemplateListItem(BaseModel):
    """One row in the GET /api/templates response. Light shape — the full
    mapping JSON is fetched separately when the user opens a template."""
    id: str
    title: str
    status: str                       # pending_review | ready | needs_attention
    is_default: bool
    created_at: str
    extra_field_count: int


class TemplateListResponse(BaseModel):
    templates: list[TemplateListItem]


class ExtraFieldDTO(BaseModel):
    """Surface-area version of models.ExtraField for the API. Same shape; we
    keep two copies to avoid forcing main.py to import the persistence
    package's models in API responses."""
    name: str
    type: str
    description: str
    pdf_field: str


class TemplateUploadResponse(BaseModel):
    """What POST /api/templates/upload returns once the AI mapping proposal
    is in. The frontend opens a review UI from this payload."""
    id: str
    title: str
    status: str
    mapping: dict                     # MappingFile.model_dump(by_alias=True)
    extra_fields: list[ExtraFieldDTO]
    field_count: int                  # how many AcroForm fields the AI saw
    # Structural validation warnings — pruned /Btn proposals, low coverage,
    # hallucinated paths. When status is "needs_attention" the frontend can
    # show these so the user knows WHY the template was flagged. Empty list
    # for clean uploads.
    warnings: list[str] = Field(default_factory=list)


# ---- Per-agent defaults (workstream C) ----

class DefaultsResponse(BaseModel):
    """GET /api/me/defaults — flat map of canonical field path → saved value.
    Empty dict for agents who haven't saved any defaults yet."""
    defaults: dict[str, str] = Field(default_factory=dict)
    # Mirror of agent_defaults.ALLOWED_DEFAULT_PATHS so the frontend can render
    # the "save as default" affordance only on eligible chips without a
    # second round-trip.
    allowed_paths: list[str] = Field(default_factory=list)


class DefaultPutRequest(BaseModel):
    """PUT /api/me/defaults/{path} body. Value is a string because we render
    everything through the existing string-template interpolate.py — number-
    typed defaults are stringified at write time so reads are uniform."""
    value: str


# ---- Voice transcription (workstream B) ----

class TranscribeResponse(BaseModel):
    """POST /api/transcribe — what the client appends to the textarea.
    seconds_used is echoed so the frontend can locally track today's quota
    without a GET round-trip; cached=true means this was served from the
    idempotency cache (no billing, no usage increment)."""
    transcript: str
    seconds_used: int
    cached: bool = False
