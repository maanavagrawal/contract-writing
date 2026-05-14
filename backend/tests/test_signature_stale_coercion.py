"""
Tests for the fill-time coercion of stale template_extras.* signature
patterns to the agent.signature / agent.initials canonical paths.

Real production mappings (the per-template JSONs the AI generates at
upload time) were captured BEFORE the signature feature shipped, so the
AI emitted template_extras.broker_agent_initials_1 etc. instead of the
new canonical paths. Re-uploading every template fixes it long-term, but
the fill pipeline can compensate retroactively so the user's existing
CAR BRBC (and every other already-uploaded template) starts stamping
signatures immediately.

The coercion runs in-memory per fill_document call — the mapping JSON
on disk is never rewritten. If we ever regenerate those mappings (e.g.
re-upload), they'll start using {agent.signature} natively and the
regex match becomes a no-op.

Critical invariant: counterparty patterns (buyer_*, seller_*, party_*,
*legally_authorized_signer*) MUST stay null. Stamping the agent's
signature on a buyer's row would be fraud.
"""
from __future__ import annotations

import pytest

from backend.generate import _signature_kind_for_template


# ---- agent-side patterns should resolve to a stamp -----------------------

@pytest.mark.parametrize("template,kind", [
    # Canonical (new mappings)
    ("{agent.signature}", "agent"),
    ("{agent.initials}", "initials"),
    # Stale: broker_agent_initials family
    ("{template_extras.broker_agent_initials_1}", "initials"),
    ("{template_extras.broker_agent_initials_5}", "initials"),
    ("{template_extras.broker_agent_initials_page3_b}", "initials"),
    ("{template_extras.broker_agent_initials_p2_line1}", "initials"),
    # Stale: agent_initials variants
    ("{template_extras.agent_initials_pg3}", "initials"),
    ("{template_extras.agent_initials_p3_left}", "initials"),
    # Stale: agent_signature family
    ("{template_extras.agent_signature_by_line_1}", "agent"),
    ("{template_extras.agent_signature_by_date_line}", "agent"),
    ("{template_extras.agent_signature_line_small}", "agent"),
    # Stale: broker_signature variants
    ("{template_extras.broker_signature_line_1}", "agent"),
    ("{template_extras.broker_signature_date_1}", "agent"),
    ("{template_extras.broker_signature_label}", "agent"),
    # Stale: by_broker_agent_signature
    ("{template_extras.by_broker_agent_signature_line}", "agent"),
])
def test_agent_signature_templates_resolve(template, kind):
    assert _signature_kind_for_template(template) == kind


# ---- counterparty patterns MUST stay null --------------------------------

@pytest.mark.parametrize("template", [
    # Buyer
    "{template_extras.buyer_signature_1}",
    "{template_extras.buyer_signature_line_3}",
    "{template_extras.buyer_signature_by_date_line}",
    "{template_extras.buyer_signed_date}",
    "{template_extras.buyer_initials_1}",
    "{template_extras.buyers_initials_field}",
    "{template_extras.buyers_initials_footer_03}",
    "{template_extras.buyer_initials_p2}",
    # Seller
    "{template_extras.seller_signature_1}",
    "{template_extras.seller_signature_line_2}",
    "{template_extras.seller_signed_date}",
    # Joint / generic party
    "{template_extras.seller_buyer_signature_1}",
    "{template_extras.party_signature_1}",
    "{template_extras.buyer_seller_landlord_tenant_signature_1}",
    # Entity authorization
    "{template_extras.entity_authorized_signers_names}",
    "{template_extras.printed_name_legally_authorized_signer}",
    "{template_extras.authorized_signer_names}",
    # CCPA multi-party
    "{template_extras.ccpa_multi_party_signature_1}",
    "{template_extras.ccpa_signature_line_1}",
    # Misc checkboxes / labels
    "{template_extras.additional_signature_addendum_checkbox}",
    "{template_extras.signature_checkbox_additional_signature_addendum}",
    "{template_extras.signature_date_line_generic}",
])
def test_counterparty_signature_templates_stay_null(template):
    assert _signature_kind_for_template(template) is None, (
        f"counterparty template promoted to agent stamp: {template}"
    )


# ---- non-signature templates pass through --------------------------------

@pytest.mark.parametrize("template", [
    "{property.address}",
    "{agent.name}",
    "{agent.brokerage}",
    "{today}",
    "{tenant_1_name}",
    "{template_extras.protection_period_days}",
    "",
    "plain text",
])
def test_non_signature_templates_return_none(template):
    assert _signature_kind_for_template(template) is None
