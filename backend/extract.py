"""
Extract structured transaction fields from notes + MLS screenshots.

Uses OpenAI Responses API with structured outputs (text_format=Pydantic) so
GPT-5 returns a validated TransactionFields instance — no regex, no JSON
parsing, no retries on malformed output.

Pillar 2 chunk 5: when active templates contribute extra_fields, this module
builds a dynamic Pydantic subclass at request time so the same single
extraction call populates BOTH the canonical schema AND a per-template
template_extras dict. Validated against the real Responses API in
scripts/spike_dynamic_schema.py — see commit 9094d80.
"""
from __future__ import annotations

import asyncio
import base64
import os

from openai import OpenAI
from pydantic import BaseModel, Field, create_model

from .models import ExtraField
from .schema import TransactionFields

# Two-tier model selection. The "full" tier runs on explicit user actions
# (paste, voice-end, generate-time re-extract) and uses gpt-5 with full
# reasoning. The "live" tier runs on debounced typing and uses gpt-5-mini
# with minimal reasoning — same SYSTEM_PROMPT, same dynamic schema, ~10x
# cheaper. Both return the same Pydantic shape; a tier mismatch can't
# silently corrupt downstream rendering.
#
# Cost gate motivation (see plan-eng-review issue 1.1): a single deal can
# fire 8-15 debounced extractions during back-and-forth editing. At 2 paying
# users x 10 deals/wk x 15 extractions/deal = 1200 calls/wk. Full gpt-5 at
# that volume = real money; live tier keeps it boring.
MODEL_FULL = "gpt-5"
MODEL_LIVE = "gpt-5-mini"

SYSTEM_PROMPT = """\
You extract structured transaction data from a real estate buyer-agent's notes \
and any attached MLS screenshots.

Notes are terse — abbreviations, dollar signs, slash-dates, single-line addresses. \
MLS screenshots are the source of truth for property details (address, beds, baths, \
list price, MLS#); the notes typically have the deal-specific bits (parties, dates, \
commission, contacts).

Rules:
  - Convert every date to ISO YYYY-MM-DD. "5/4/26" means May 4, 2026.
  - Keep money strings as the agent wrote them. "$3182" stays "$3182". Don't add commas.
  - Strip surrounding qualifiers from money: "co-op $3182" → "$3182", "approx $725k" → "$725k". \
    Just the dollar (or percent) amount itself.
  - For transaction_type, choose 'lease' if you see monthly rent / co-op / lease dates / tenant; \
    'sale' if you see purchase price / closing / buyer-seller; null if genuinely ambiguous.
  - Property address: street + number only (e.g. "221 W Hubbard"). Unit goes in unit. \
    Never put the unit in address.
  - Tenant/buyer names: one entry per person. If notes don't mention names, return [].
  - landlord_billing_email: pick the email that's clearly for invoicing/leasing/management, \
    not the buyer's personal email.
  - concessions: free-form like "1 month free" or "no security deposit". Null if none mentioned.
  - net_monthly_rent: only set if explicitly different from monthly_rent (e.g. notes mention "net" \
    after concessions). Otherwise null.
  - protection_period_days / early_termination_fee: only set if the notes explicitly mention them. \
    Otherwise null and the form will default to 30 days / $0.

Sale-specific:
  - seller_names: list, one per person. Empty for leases.
  - county: just the county name (e.g. 'Cook'), no 'County' suffix.
  - property_type: 'attached' for townhouse / row / condo townhome, 'detached' for SFH, \
    'multi_unit' for 2-4 unit buildings.
  - earnest_money / additional_earnest_money: dollar amounts as written.
  - additional_earnest_date: only if notes mention a second earnest tender date.
  - loan_type/loan_rate_type/loan_percent_of_price/loan_max_rate/loan_amortization_years: \
    only set if explicitly mentioned. Otherwise null and the form defaults (conventional, fixed, \
    80%, 30 years, 1 point) apply.
  - buyer_address/city/state/zip: buyer's CURRENT residence (the address they want documents \
    delivered to). Often same as property address but not always.

  - When in doubt, return null. Better empty than wrong — the agent reviews before generating.
"""

_client: OpenAI | None = None


# ExtraField.type → (python type, friendly description for the prompt).
# Every field is nullable — extraction is best-effort and the agent reviews.
_TYPE_MAP: dict[str, tuple[type, str]] = {
    "text":     (str | None,       "free-form string, as written by the agent"),
    "money":    (str | None,       "money string preserving agent formatting (e.g. '$3,182' or '2.5%')"),
    "date":     (str | None,       "ISO YYYY-MM-DD date"),
    "number":   (int | None,       "integer count"),
    "bool":     (bool | None,      "true/false"),
    "list_str": (list[str] | None, "list of strings, one entry per item"),
}


def _build_extras_model_for_template(template_id: str, extras: list[ExtraField]) -> type[BaseModel]:
    """One Pydantic model per active template, holding its extra_fields. The
    model name is namespaced by template_id so two uploaded templates with
    overlapping extra-field names (e.g. both have 'note') don't clobber each
    other in the dynamic schema."""
    field_defs: dict[str, tuple] = {}
    for ef in extras:
        py_type, type_hint = _TYPE_MAP.get(ef.type, _TYPE_MAP["text"])
        # Build a Field with the AI-author's description (set during upload)
        # plus the type hint so the AI knows the expected shape.
        desc = (ef.description or "").strip()
        if desc:
            full_desc = f"{desc} ({type_hint})"
        else:
            full_desc = type_hint
        field_defs[ef.name] = (py_type, Field(None, description=full_desc))

    # Sanitize template_id for class name (alphanumeric + underscore only).
    safe_id = "".join(c if c.isalnum() else "_" for c in template_id)
    return create_model(f"Extras_{safe_id}", **field_defs)


def build_dynamic_extraction_model(
    template_extras: dict[str, list[ExtraField]],
) -> type[BaseModel]:
    """Return a TransactionFields subclass with a `template_extras` field
    keyed by template_id. Empty dict = return TransactionFields directly so
    the JSON schema doesn't carry a useless empty container.

    Mirrors what scripts/spike_dynamic_schema.py validated against the real
    API. Verified shapes work for 0/1/2 active templates, 22-token cache hit
    rate, no strict-mode 400s.
    """
    if not template_extras:
        return TransactionFields

    container_fields: dict[str, tuple] = {}
    for template_id, extras in template_extras.items():
        if not extras:
            continue
        extras_model = _build_extras_model_for_template(template_id, extras)
        # Sanitize key so it's a valid Python identifier on the container model.
        safe_key = "".join(c if c.isalnum() else "_" for c in template_id)
        container_fields[safe_key] = (
            extras_model | None,
            Field(None, description=f"Extra fields contributed by template '{template_id}'"),
        )

    if not container_fields:
        return TransactionFields

    container_model = create_model("TemplateExtrasContainer", **container_fields)
    return create_model(
        "TransactionFieldsExtended",
        __base__=TransactionFields,
        template_extras=(
            container_model | None,
            Field(None, description="Per-template extra fields. Populate the sub-object only when the agent's notes mention values relevant to that template."),
        ),
    )


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        _client = OpenAI(api_key=api_key)
    return _client


def _image_to_data_url(content: bytes, mime: str) -> str:
    b64 = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def extract_fields(
    notes: str,
    images: list[tuple[bytes, str]] | None = None,
    template_extras: dict[str, list[ExtraField]] | None = None,
    tier: str = "full",
) -> BaseModel:
    """
    images: list of (bytes, mime_type) tuples. Empty/None is fine.
    template_extras: maps active template_id → its extra_fields. Passing an
        empty dict (or omitting) gives the original TransactionFields-only
        behavior. Passing one or more templates promotes the schema to
        TransactionFieldsExtended with a template_extras nested object.
    tier: "full" (gpt-5, default) for user-driven extractions; "live"
        (gpt-5-mini) for debounced typing-triggered extractions. Same
        SYSTEM_PROMPT and dynamic schema for both — a tier swap can't change
        the response shape, only the model behind it. Live tier skips images
        because the typing-trigger path doesn't add new screenshots.

    Returns a Pydantic instance of either TransactionFields or
    TransactionFieldsExtended depending on whether any extras were active.
    Frontend treats both shapes as identical except for the optional
    template_extras key.
    """
    user_content: list[dict] = []
    if notes.strip():
        user_content.append({"type": "input_text", "text": f"Agent notes:\n\n{notes.strip()}"})
    else:
        user_content.append({"type": "input_text", "text": "(No text notes provided.)"})

    # Live tier never carries images — debounced-typing path can't add new
    # screenshots, so we save bandwidth and the heavier vision-tier model.
    if tier != "live":
        for content, mime in images or []:
            user_content.append({
                "type": "input_image",
                "image_url": _image_to_data_url(content, mime),
            })

    schema_model = build_dynamic_extraction_model(template_extras or {})

    # Model selection:
    #   - tier="live": always mini (no images, debounced path).
    #   - tier="full" without images: mini too. The user-reported 80s extraction
    #     on a notes-only paste was gpt-5; mini handles structured extraction
    #     from plain text with effectively the same quality at 3-4× speed.
    #   - tier="full" with images: gpt-5 stays. MLS screenshots need the
    #     stronger vision model; mini's vision is weaker and we'd see address
    #     / price extraction regress.
    has_images = bool(images) and tier != "live"
    if tier == "live":
        model_id = MODEL_LIVE
    elif has_images:
        model_id = MODEL_FULL
    else:
        model_id = MODEL_LIVE

    # reasoning_effort="low" — same rationale as templates._propose_mapping_chunk.
    # This is structured extraction with a Pydantic schema; the model is
    # picking values from text, not reasoning. Default "high" added 20-50s
    # of overhead per call with zero quality gain.
    # On gpt-5 the full tier still has access to images and full vocab;
    # "low" just skips the reasoning pre-pass. Quality verified against
    # eval suite.
    client = _get_client()
    response = await asyncio.to_thread(
        client.responses.parse,
        model=model_id,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        text_format=schema_model,
        reasoning={"effort": "low"},
    )

    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("model returned no parsed output")
    return parsed
