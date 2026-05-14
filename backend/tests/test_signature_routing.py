"""
Tests for signature/initials routing in the AI mapping post-processor.

Covers the new _EXTRA_TO_CANONICAL_RULES entries for agent.signature and
agent.initials, plus the negative cases that MUST stay null (counterparty
signatures + initials → those are wet-signed by clients, not stamped by us).

This is the eng-review-required guard against the 93% MIN regression risk:
if the coercion accidentally promotes too aggressively, the eval will
catch it — but unit tests catch it cheaper and faster.
"""
from __future__ import annotations

from backend.templates import (
    _CANONICAL_PATH_ALLOWLIST,
    _coerce_extra_to_canonical,
)


def test_signature_canonical_paths_are_in_allowlist():
    """Without this, _coerce_extra_to_canonical would silently fail to
    promote signature/initials extras because the allowlist check at the
    end of the function rejects them."""
    assert "agent.signature" in _CANONICAL_PATH_ALLOWLIST
    assert "agent.initials" in _CANONICAL_PATH_ALLOWLIST


# ---- Promotion: agent-side signatures should route to agent.signature -----

def test_agent_signature_extra_promotes():
    assert _coerce_extra_to_canonical("agent_signature_by_line_1") == "agent.signature"


def test_broker_signature_extra_promotes():
    assert _coerce_extra_to_canonical("broker_signature") == "agent.signature"


def test_salesperson_signed_extra_promotes():
    assert _coerce_extra_to_canonical("salesperson_signed_date") == "agent.signature"


def test_signature_by_pattern_promotes():
    """The 'By (Broker/Agent) Signature' field pattern is the most common
    way CAR/Multi-Board forms surface the agent signature slot."""
    assert _coerce_extra_to_canonical("signature_by_field") == "agent.signature"


# ---- Promotion: agent-side initials should route to agent.initials -------

def test_agent_initials_extra_promotes():
    assert _coerce_extra_to_canonical("agent_initials_page_1") == "agent.initials"


def test_broker_initial_box_promotes():
    assert _coerce_extra_to_canonical("broker_initial_box") == "agent.initials"


# ---- Non-promotion: counterparty signatures must stay handfill extras ----

def test_buyer_signature_does_not_promote():
    """Buyer signs separately — wet ink or external e-sign. Stamping the
    agent's signature here would be FRAUD. Must return None."""
    assert _coerce_extra_to_canonical("buyer_signature_line") is None


def test_seller_signature_does_not_promote():
    assert _coerce_extra_to_canonical("seller_signature_1") is None


def test_tenant_signature_does_not_promote():
    assert _coerce_extra_to_canonical("tenant_signature_field") is None


def test_landlord_signature_does_not_promote():
    assert _coerce_extra_to_canonical("landlord_signature_date") is None


def test_buyer_initials_does_not_promote():
    assert _coerce_extra_to_canonical("buyer_initials_1") is None


def test_seller_initials_does_not_promote():
    assert _coerce_extra_to_canonical("seller_initials_box") is None


# ---- Non-promotion: ambiguous/generic signature extras stay null ---------

def test_bare_signature_does_not_promote():
    """A bare 'signature' extra with no agent/broker/by qualifier could be
    anyone's signature. Stay safe — the existing handfill suppression in
    _HANDFILL_NAME_TOKENS catches these for the low-confidence banner."""
    assert _coerce_extra_to_canonical("signature") is None


def test_bare_initials_does_not_promote():
    assert _coerce_extra_to_canonical("initials") is None


def test_signature_date_does_not_promote():
    """Date fields on a signature row are a 'today' canonical (existing
    rule, untouched by this feature). The coercion should NOT route them
    to agent.signature just because the extra name contains 'signature'."""
    # "signature_date" alone has no agent/broker/by qualifier → stays null.
    # (It'll match the existing handfill suppression at fill time.)
    assert _coerce_extra_to_canonical("signature_date") is None


# ---- Sanity: existing rules untouched by the new ones --------------------

def test_existing_county_rule_still_works():
    assert _coerce_extra_to_canonical("covered_counties_list_1") == "county"


def test_existing_brokerage_license_rule_still_works():
    assert _coerce_extra_to_canonical("brokerage_lic_number") == "agent.brokerage_license"
