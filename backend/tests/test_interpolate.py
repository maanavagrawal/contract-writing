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
