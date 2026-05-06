"""
Extract structured transaction fields from notes + MLS screenshots.

Uses OpenAI Responses API with structured outputs (text_format=Pydantic) so
GPT-5 returns a validated TransactionFields instance — no regex, no JSON
parsing, no retries on malformed output.
"""
from __future__ import annotations

import asyncio
import base64
import os

from openai import OpenAI

from .schema import TransactionFields

MODEL = "gpt-5"

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
) -> TransactionFields:
    """
    images: list of (bytes, mime_type) tuples. Empty/None is fine.
    Returns a validated TransactionFields (fields the model couldn't infer are null).
    """
    user_content: list[dict] = []
    if notes.strip():
        user_content.append({"type": "input_text", "text": f"Agent notes:\n\n{notes.strip()}"})
    else:
        user_content.append({"type": "input_text", "text": "(No text notes provided.)"})

    for content, mime in images or []:
        user_content.append({
            "type": "input_image",
            "image_url": _image_to_data_url(content, mime),
        })

    client = _get_client()
    response = await asyncio.to_thread(
        client.responses.parse,
        model=MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        text_format=TransactionFields,
    )

    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("model returned no parsed output")
    return parsed
