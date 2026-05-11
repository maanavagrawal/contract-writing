"""
Tiny template engine for mapping JSON files.

Pattern: {path.to.field} or {path.to.field|filter}
  - dot paths walk dicts: {property.address} → ctx["property"]["address"]
  - missing or null values render as ""
  - filters available: date, currency, upper, join

Why not Jinja: one dependency, one syntax to teach, ~60 lines. The mapping
JSONs only need primitive substitution.

mapping.fields value types
==========================
A mapping value can be one of two shapes — interpolate_mapping() dispatches:

  "{property.address}"   string template: walks ctx via _resolve + filters.
  "/Choice1"             literal AcroForm state name: passed through unchanged
                          for fill_pdf to write to /AS.
  BtnChoice(...)         conditional state lookup. Used for /Btn fields on
                          autonomously-mapped templates where the widget's
                          exact /AP/N keys aren't '/On'. Resolves to the
                          state name matching ctx[canonical_path], or '/Off'
                          when missing/unknown.

Conditionals deliberately live in BtnChoice rather than in the template
language: they're domain-specific (only /Btn fields need them) and we want
schema-level validation that the keys/values match the canonical Literal
enum + the widget's /AP/N keys at upload time, not at fill time.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .schema import BtnChoice

_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)(?:\|([a-zA-Z_]+))?\}")


def _resolve(ctx: dict[str, Any], path: str) -> Any:
    """Walk a dotted path through nested dicts. Returns None on miss."""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def _format_date(value: Any) -> str:
    """ISO YYYY-MM-DD → MM/DD/YYYY. Pass through anything we can't parse."""
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%m/%d/%Y")
    s = str(value).strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return s


def _format_currency(value: Any) -> str:
    """Ensure $ prefix and thousands commas. Idempotent on already-formatted strings."""
    if value is None or value == "":
        return ""
    s = str(value).strip().lstrip("$").replace(",", "")
    try:
        n = float(s)
        return f"${n:,.0f}" if n == int(n) else f"${n:,.2f}"
    except ValueError:
        return str(value)


def _format_join(value: Any) -> str:
    """List → 'A & B' for two, 'A, B, C' for three+, or just A for one."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    items = [str(x).strip() for x in value if x]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} & {items[1]}"
    return ", ".join(items)


_FILTERS = {
    "date": _format_date,
    "currency": _format_currency,
    "upper": lambda v: str(v).upper() if v else "",
    "join": _format_join,
}


def interpolate(template: str, ctx: dict[str, Any]) -> str:
    """Substitute every {path} (with optional |filter) in template against ctx."""

    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        filter_name = match.group(2)
        value = _resolve(ctx, path)
        if filter_name:
            fn = _FILTERS.get(filter_name)
            if fn is None:
                raise ValueError(f"unknown filter: {filter_name}")
            return fn(value)
        if value is None:
            return ""
        if isinstance(value, list):
            return _format_join(value)
        return str(value)

    return _PATTERN.sub(replace, template)


def _split_date(iso: str | None) -> tuple[str, str]:
    """ISO YYYY-MM-DD → ('M/D', 'YY'). ('', '') if unparseable."""
    if not iso:
        return ("", "")
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
    except ValueError:
        return ("", "")
    return (f"{d.month}/{d.day}", str(d.year)[-2:])


def build_context(fields_dict: dict[str, Any], agent_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten the request into a single context dict for templates, and add
    computed convenience fields ({property.address_full}, {today}, etc.).
    """
    ctx: dict[str, Any] = dict(fields_dict)
    ctx["agent"] = agent_dict

    today = date.today()
    ctx["today"] = today.strftime("%m/%d/%Y")
    ctx["today_month_day"] = f"{today.month}/{today.day}"
    ctx["today_year_2digit"] = str(today.year)[-2:]

    prop = ctx.get("property") or {}
    if isinstance(prop, dict):
        addr = prop.get("address") or ""
        unit = prop.get("unit") or ""
        city = prop.get("city") or ""
        state = prop.get("state") or ""
        zip_ = prop.get("zip") or ""
        parts: list[str] = []
        if addr:
            parts.append(f"{addr} Unit {unit}" if unit else addr)
        cs = ", ".join(p for p in [city, f"{state} {zip_}".strip()] if p.strip())
        if cs:
            parts.append(cs)
        prop["address_full"] = ", ".join(parts)
        ctx["property"] = prop

    # County suffix: ", Cook County" if a county is provided, else "". Lets the
    # Multi-Board Address line render cleanly when county is missing instead of
    # leaving a literal " County" tail.
    county = (ctx.get("county") or "").strip()
    ctx["county_suffix"] = f", {county} County" if county else ""

    # Split tenant/buyer name list into 1st and 2nd individuals.
    names = ctx.get("tenant_or_buyer_names") or []
    if isinstance(names, list):
        ctx["tenant_1_name"] = names[0] if len(names) >= 1 else ""
        ctx["tenant_2_name"] = names[1] if len(names) >= 2 else ""

    # Split lease_end into M/D and YY for the Tenant Rep termination row.
    md, yy = _split_date(ctx.get("lease_end"))
    ctx["lease_end_month_day"] = md
    ctx["lease_end_year_2digit"] = yy

    # Defaults are gated on transaction_type to prevent cross-type leaks.
    # Before this gate (2026-05-10 incident), sale-side defaults like
    # `loan_percent_of_price=80` and `tax_proration_percent=105` fired on
    # every deal — including leases — and rendered into whatever fields the
    # template happened to map there. Result: lease PDFs showed "80% loan,
    # 30-year amortization, 105% tax proration" with no opt-out.
    # Strict gate: defaults only when transaction_type matches.
    tx_type = (ctx.get("transaction_type") or "").lower()

    if tx_type == "lease":
        # Net rent defaults to monthly rent when blank.
        if not ctx.get("net_monthly_rent"):
            ctx["net_monthly_rent"] = ctx.get("monthly_rent") or ""

        # Sensible defaults for the Tenant Rep boilerplate.
        if not ctx.get("protection_period_days"):
            ctx["protection_period_days"] = "30"
        if not ctx.get("early_termination_fee"):
            ctx["early_termination_fee"] = "$0"
        if not ctx.get("retainer"):
            ctx["retainer"] = "$0"

    if tx_type == "sale":
        # ---- Multi-Board sale defaults ----
        if not ctx.get("earnest_business_days"):
            ctx["earnest_business_days"] = "5"
        if not ctx.get("loan_type"):
            ctx["loan_type"] = "conventional"
        if not ctx.get("loan_rate_type"):
            ctx["loan_rate_type"] = "fixed"
        if not ctx.get("loan_percent_of_price"):
            ctx["loan_percent_of_price"] = "80"
        if not ctx.get("loan_amortization_years"):
            ctx["loan_amortization_years"] = "30"
        if not ctx.get("loan_max_points"):
            ctx["loan_max_points"] = "1"
        if not ctx.get("tax_proration_percent"):
            # Cook County standard is 110%; elsewhere often 105%.
            ctx["tax_proration_percent"] = "110" if (ctx.get("county") or "").lower().startswith("cook") else "105"
        if not ctx.get("escrowee"):
            ctx["escrowee"] = "seller"

    # Buyer/seller name joins for the contract.
    seller_names = ctx.get("seller_names") or []
    if isinstance(seller_names, list):
        ctx["seller_names_joined"] = _format_join(seller_names)
    ctx["buyer_names_joined"] = _format_join(ctx.get("tenant_or_buyer_names") or [])

    # Buyer's current residence as a single line for the City/State/Zip slot.
    parts = []
    for k in ("buyer_city", "buyer_state", "buyer_zip"):
        v = ctx.get(k)
        if v:
            parts.append(str(v))
    ctx["buyer_city_state_zip"] = ", ".join(parts[:1] + ([" ".join(parts[1:])] if parts[1:] else []))

    # Closing date split (M/D and 2-digit year).
    md, yy = _split_date(ctx.get("closing_date"))
    ctx["closing_date_month_day"] = md
    ctx["closing_date_year_2digit"] = yy

    # Additional earnest tender date split.
    md, yy = _split_date(ctx.get("additional_earnest_date"))
    ctx["additional_earnest_month_day"] = md
    ctx["additional_earnest_year_2digit"] = yy

    # ---- Checkbox / radio state names (Multi-Board AcroForm AS values) ----
    # Property type: 3 separate /On checkboxes — exactly one fires.
    pt = ctx.get("property_type")
    ctx["property_type_attached_state"] = "/On" if pt == "attached" else ""
    ctx["property_type_detached_state"] = "/On" if pt == "detached" else ""
    ctx["property_type_multi_unit_state"] = "/On" if pt == "multi_unit" else ""

    # Escrowee radio group (field 31).
    ctx["escrowee_state"] = {
        "seller": "/Seller's Brokerage",
        "buyer": "/Buyer's Brokerage",
        "other": "/As otherwise agreed",
    }.get(ctx.get("escrowee") or "seller", "/Seller's Brokerage")

    # Seller-pays-Buyer-Brokerage option (field 37): /Amount or /Percent.
    # Heuristic: percent if commission_amount ends in '%', dollar otherwise.
    comm = (ctx.get("commission_amount") or "").strip()
    is_percent = comm.endswith("%")
    ctx["seller_pays_brokerage_state"] = "/Percent" if is_percent else ("/Amount" if comm else "")
    # Field 38 is the % column on L33; field 39 is the $ column on L34.
    # Only fill the column matching the chosen state — leaving the other blank
    # avoids confusing both columns showing values.
    ctx["commission_percent_value"] = comm.rstrip("%").strip() if is_percent else ""
    ctx["commission_dollar_value"] = "" if is_percent else (
        _format_currency(comm) if comm else ""
    )

    # Financing rate type (field 111).
    ctx["loan_rate_type_state"] = {"fixed": "/Fixed", "adjustable": "/Adjustable"}.get(
        ctx.get("loan_rate_type") or "fixed", "/Fixed"
    )
    # Financing loan type (field 112): /Choice1..5 = conv, FHA, VA, USDA, other.
    ctx["loan_type_state"] = {
        "conventional": "/Choice1",
        "fha": "/Choice2",
        "va": "/Choice3",
        "usda": "/Choice4",
        "other": "/Choice5",
    }.get(ctx.get("loan_type") or "conventional", "/Choice1")

    # Statutory disclosure has/has-not radios (132, 133, 138, 139, 140).
    # Default: if the agent has done their job, all "Has" before signing.
    ctx["statutory_state"] = "/Has"

    return ctx


def resolve_btn_choice(ctx: dict[str, Any], bc: BtnChoice) -> str:
    """Resolve a BtnChoice into a literal /Btn state name for fill_pdf.

    Algorithm:
      1. Look up ctx[canonical_path] via _resolve (handles dotted paths).
      2. Normalize the value to a string key (booleans → "true"/"false";
         everything else → str(value)).
      3. If the key is in bc.choices, return that state name.
      4. Otherwise return "/Off" — never falsely fire a widget.

    The "/Off" fallback handles three cases identically: missing ctx key,
    None value, and value not in the choices table. All three should leave
    the widget unchecked. fill_pdf already handles a "/Off" string by
    writing /Off to every widget kid's /AS.
    """
    value = _resolve(ctx, bc.canonical_path)
    if value is None:
        return "/Off"
    if isinstance(value, bool):
        # bool is a subclass of int — check first so True doesn't fall into
        # the float branch as 1.0. Lowercase string form so mapping JSONs use
        # the natural shape {"true": "/On", "false": "/Off"}.
        key = "true" if value else "false"
    elif isinstance(value, float) and value.is_integer():
        # 1.0 → "1" not "1.0" so AI-friendly integer keys still match when
        # a value round-trips through JSON as a float (common in template_extras).
        key = str(int(value))
    else:
        key = str(value)
    return bc.choices.get(key, "/Off")


def interpolate_mapping(
    mapping_fields: dict[str, str | BtnChoice],
    ctx: dict[str, Any],
) -> dict[str, str]:
    """Render every mapping value against ctx, returning a flat
    {pdf_field: rendered_string} dict for fill_pdf.

    Dispatches per value type:
      - str → interpolate() (existing string-template path)
      - BtnChoice → resolve_btn_choice() (conditional state lookup)

    This is the single entry point generate.py uses; it keeps both rendering
    paths in one module and gives tests a clean unit boundary.
    """
    out: dict[str, str] = {}
    for pdf_field, value in mapping_fields.items():
        if isinstance(value, BtnChoice):
            out[pdf_field] = resolve_btn_choice(ctx, value)
        else:
            out[pdf_field] = interpolate(value, ctx)
    return out
