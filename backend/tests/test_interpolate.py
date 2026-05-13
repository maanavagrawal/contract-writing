"""
Tests for backend/interpolate.build_context default gating.

Regression test for the 2026-05-10 incident: sale defaults (loan_*, tax_*,
escrowee, earnest_business_days) leaked into lease deals because the
defaults block fired unconditionally. After the gate, defaults only fire
when transaction_type matches.
"""
from __future__ import annotations

from backend.interpolate import build_context


def _ctx(transaction_type=None, **overrides):
    """Helper: build a minimal fields dict for build_context."""
    fields = {
        "transaction_type": transaction_type,
        "property": {"address": "221 W Hubbard"},
    }
    fields.update(overrides)
    return build_context(fields, {"name": "Test Agent"})


SALE_DEFAULT_KEYS = (
    "earnest_business_days",
    "loan_type",
    "loan_rate_type",
    "loan_percent_of_price",
    "loan_amortization_years",
    "loan_max_points",
    "tax_proration_percent",
    "escrowee",
)

LEASE_DEFAULT_KEYS = (
    "protection_period_days",
    "early_termination_fee",
    "retainer",
)


# ---- Sale branch ----

def test_sale_defaults_apply_when_transaction_type_sale():
    ctx = _ctx(transaction_type="sale")
    assert ctx["earnest_business_days"] == "5"
    assert ctx["loan_type"] == "conventional"
    assert ctx["loan_rate_type"] == "fixed"
    assert ctx["loan_percent_of_price"] == "80"
    assert ctx["loan_amortization_years"] == "30"
    assert ctx["loan_max_points"] == "1"
    assert ctx["escrowee"] == "seller"


def test_sale_defaults_skip_when_transaction_type_lease():
    """REGRESSION: user's reported bug. Lease deal must not get sale defaults."""
    ctx = _ctx(transaction_type="lease")
    for key in SALE_DEFAULT_KEYS:
        assert key not in ctx or not ctx[key], (
            f"Sale default {key!r} leaked into lease deal "
            f"(got {ctx.get(key)!r}). This was the 2026-05-10 incident."
        )


def test_sale_defaults_skip_when_transaction_type_null():
    """Strict gate: null transaction_type produces no defaults."""
    ctx = _ctx(transaction_type=None)
    for key in SALE_DEFAULT_KEYS:
        assert key not in ctx or not ctx[key], (
            f"Sale default {key!r} fired on null transaction_type"
        )


# ---- Lease branch ----

def test_lease_defaults_apply_when_transaction_type_lease():
    ctx = _ctx(transaction_type="lease")
    assert ctx["protection_period_days"] == "30"
    assert ctx["early_termination_fee"] == "$0"
    assert ctx["retainer"] == "$0"


def test_lease_defaults_skip_when_transaction_type_sale():
    """Symmetric: sale deal must not get lease defaults."""
    ctx = _ctx(transaction_type="sale")
    for key in LEASE_DEFAULT_KEYS:
        assert key not in ctx or not ctx[key], (
            f"Lease default {key!r} leaked into sale deal"
        )


def test_lease_defaults_skip_when_transaction_type_null():
    ctx = _ctx(transaction_type=None)
    for key in LEASE_DEFAULT_KEYS:
        assert key not in ctx or not ctx[key], (
            f"Lease default {key!r} fired on null transaction_type"
        )


# ---- Cook County tax proration edge case ----

def test_tax_proration_cook_county_is_110():
    """Cook County uses 110% as the standard tax proration."""
    ctx = _ctx(transaction_type="sale", county="Cook")
    assert ctx["tax_proration_percent"] == "110"


def test_tax_proration_other_county_is_105():
    """Non-Cook counties default to 105%."""
    ctx = _ctx(transaction_type="sale", county="DuPage")
    assert ctx["tax_proration_percent"] == "105"


# ---- Existing-value preservation ----

def test_explicit_values_not_overwritten_by_defaults():
    """If the user (or extraction) provided a value, defaults must not stomp it."""
    ctx = _ctx(
        transaction_type="sale",
        loan_percent_of_price="75",
        loan_amortization_years="15",
        tax_proration_percent="100",
    )
    assert ctx["loan_percent_of_price"] == "75"
    assert ctx["loan_amortization_years"] == "15"
    assert ctx["tax_proration_percent"] == "100"


# ============================================================================
# BtnChoice + interpolate_mapping (autonomous /Btn fill via AI-generated tables)
# ============================================================================
#
# Background: bundled canonical mappings encode /Btn state names via computed
# values like {property_type_attached_state}. That works only because we wrote
# those mappings by hand against a specific PDF. For autonomously-mapped user
# uploads we don't know the widget /AP/N keys until upload time, so the AI
# emits a BtnChoice table that says "for ctx[canonical_path] == X, write
# state Y". interpolate_mapping resolves both shapes (string templates AND
# BtnChoice) into a flat dict for fill_pdf.

from backend.interpolate import interpolate_mapping, resolve_btn_choice
from backend.schema import BtnChoice


def test_btnchoice_resolves_to_matching_state():
    """ctx[canonical_path] matches a key in choices -> emit that state."""
    bc = BtnChoice(
        canonical_path="property_type",
        choices={"attached": "/On", "detached": "/Off", "multi_unit": "/Off"},
    )
    assert resolve_btn_choice({"property_type": "attached"}, bc) == "/On"


def test_btnchoice_resolves_to_off_when_value_not_in_choices():
    """If ctx[path] isn't in choices, emit /Off — never falsely fire a widget."""
    bc = BtnChoice(
        canonical_path="property_type",
        choices={"attached": "/On"},
    )
    assert resolve_btn_choice({"property_type": "detached"}, bc) == "/Off"


def test_btnchoice_resolves_to_off_when_ctx_value_missing():
    """Missing key or None value -> /Off."""
    bc = BtnChoice(
        canonical_path="property_type",
        choices={"attached": "/On"},
    )
    assert resolve_btn_choice({}, bc) == "/Off"
    assert resolve_btn_choice({"property_type": None}, bc) == "/Off"


def test_btnchoice_handles_radio_group_with_multiple_states():
    """Real radio group: one field, N kids with N different /AP/N states.
    Each canonical value resolves to its own widget state."""
    bc = BtnChoice(
        canonical_path="escrowee",
        choices={
            "seller": "/Seller's Brokerage",
            "buyer": "/Buyer's Brokerage",
            "other": "/As otherwise agreed",
        },
    )
    assert resolve_btn_choice({"escrowee": "seller"}, bc) == "/Seller's Brokerage"
    assert resolve_btn_choice({"escrowee": "buyer"}, bc) == "/Buyer's Brokerage"
    assert resolve_btn_choice({"escrowee": "other"}, bc) == "/As otherwise agreed"


def test_btnchoice_handles_bool_canonical_value():
    """A canonical bool (e.g. template_extras.dual_agency) lowercases to
    'true'/'false' for the choices lookup."""
    bc = BtnChoice(
        canonical_path="template_extras.dual_agency",
        choices={"true": "/On"},
    )
    ctx = {"template_extras": {"dual_agency": True}}
    assert resolve_btn_choice(ctx, bc) == "/On"
    ctx_false = {"template_extras": {"dual_agency": False}}
    assert resolve_btn_choice(ctx_false, bc) == "/Off"


def test_btnchoice_integer_float_round_trip_normalization():
    """REGRESSION (adversarial review 2026-05-10): a template_extras int value
    can round-trip through JSON as a float (1 → 1.0). AI uses string keys like
    '1' in choices. Without normalization, str(1.0)='1.0' wouldn't match '1'
    → silent /Off. Normalize int-valued floats to their integer string form."""
    bc = BtnChoice(canonical_path="num_kids", choices={"1": "/On", "2": "/Off"})
    assert resolve_btn_choice({"num_kids": 1}, bc) == "/On"
    assert resolve_btn_choice({"num_kids": 1.0}, bc) == "/On"  # the regression
    assert resolve_btn_choice({"num_kids": 2}, bc) == "/Off"
    # Non-integer float still uses str() — '1.5' won't match '1' or '2'.
    assert resolve_btn_choice({"num_kids": 1.5}, bc) == "/Off"


def test_interpolate_mapping_dispatches_str_and_btnchoice():
    """interpolate_mapping is the entry point generate.py uses. It must
    correctly dispatch between string templates and BtnChoice."""
    ctx = {
        "property": {"address": "221 W Hubbard"},
        "property_type": "attached",
    }
    fields: dict[str, object] = {
        "FIELD_A": "{property.address}",
        "FIELD_B": BtnChoice(
            canonical_path="property_type",
            choices={"attached": "/On"},
        ),
        "FIELD_C": "literal string passes through",
    }
    rendered = interpolate_mapping(fields, ctx)
    assert rendered["FIELD_A"] == "221 W Hubbard"
    assert rendered["FIELD_B"] == "/On"
    assert rendered["FIELD_C"] == "literal string passes through"


def test_interpolate_mapping_preserves_existing_string_only_mappings():
    """REGRESSION: bundled canonical mappings (multiboard.json etc.) are
    pure dict[str, str]. interpolate_mapping must return identical results
    for them as the old `dict comprehension` approach did."""
    ctx = {
        "property": {"address": "221 W Hubbard"},
        "property_type_attached_state": "/On",
        "purchase_price": "725000",
    }
    fields: dict[str, object] = {
        "ADDR": "{property.address}",
        "TYPE_BOX": "{property_type_attached_state}",
        "PRICE": "{purchase_price|currency}",
        "EMPTY": "",
    }
    rendered = interpolate_mapping(fields, ctx)
    assert rendered["ADDR"] == "221 W Hubbard"
    assert rendered["TYPE_BOX"] == "/On"
    assert rendered["PRICE"] == "$725,000"
    assert rendered["EMPTY"] == ""


# ---- template_extras plumbing (CAR BRBC fix 2026-05-12) ----

def test_template_extras_resolves_in_context():
    """REGRESSION: before this fix, mapping strings like
    {template_extras.brbc_compensation_percent} silently rendered blank
    because build_context never received the extras dict. Confirmed end-to-end
    via interpolate_mapping."""
    from backend.interpolate import interpolate_mapping
    ctx = build_context(
        {"property": {"address": "221 W Hubbard"}},
        {"name": "Test Agent"},
        template_extras={"brbc_compensation_percent": "2.5", "brbc_cities_list": "Oakland"},
    )
    rendered = interpolate_mapping({
        "F1": "{template_extras.brbc_compensation_percent}",
        "F2": "{template_extras.brbc_cities_list}",
        "F3": "{template_extras.missing}",
    }, ctx)
    assert rendered["F1"] == "2.5"
    assert rendered["F2"] == "Oakland"
    assert rendered["F3"] == ""  # missing key → blank, not crash


def test_template_extras_defaults_to_empty_when_omitted():
    """build_context called without template_extras must still produce a ctx
    that resolves {template_extras.X} to blank (not KeyError)."""
    from backend.interpolate import interpolate_mapping
    ctx = build_context({"property": {}}, {"name": "Agent"})
    rendered = interpolate_mapping({"F": "{template_extras.anything}"}, ctx)
    assert rendered["F"] == ""
