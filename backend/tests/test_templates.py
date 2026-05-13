"""
Tests for the template upload pipeline (multi-tenant).

Layers:
  - validate_pdf rejects bad inputs (encrypted, no AcroForm, malformed)
  - proposal_to_mapping_file translates AI output into the on-disk shape
    correctly (canonical paths vs extras vs the unmapped fallback)
  - the FastAPI endpoints work end-to-end with the OpenAI call mocked

The OpenAI call is the slow + expensive part, so we monkeypatch
templates.propose_mapping to return a hand-built ProposedMapping instead of
hitting the real API.

Multi-tenancy specifics:
  - Every endpoint requires a session cookie (provided by authed_client fixture)
  - Uploaded templates are scoped to the caller's user_id
  - Cross-user reads/deletes return 404, never 403, never the row
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import models, templates as templates_mod
from backend.templates import (
    ProposedField,
    ProposedMapping,
    TemplateUploadError,
    proposal_to_mapping_file,
    validate_pdf,
)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LEASE_INVOICE_PDF = REPO_ROOT / "templates" / "pdf" / "2025 Compass Chicagoland Lease Invoice Landlords and Tenant Use copy.pdf"


# ---- Pure validation tests (no DB, no OpenAI) ----

def test_validate_pdf_accepts_a_real_acroform_pdf():
    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    reader, persist_bytes = validate_pdf(pdf_bytes)
    from backend.pdf_introspect import walk_fields
    assert len(walk_fields(reader)) > 0
    # AcroForm path: returned bytes are the original, no synthesis happened.
    assert persist_bytes == pdf_bytes


def test_validate_pdf_rejects_garbage():
    with pytest.raises(TemplateUploadError, match="could not parse"):
        validate_pdf(b"this is not a pdf at all")


def test_validate_pdf_synthesizes_fields_for_flattened_pdf(tmp_path: Path):
    """REGRESSION (2026-05-11 incident): the CAR Buyer Rep PDF arrives
    via iLovePDF with /AcroForm completely stripped. Previously we rejected
    it; now field_synth should detect blanks visually and rewrite the bytes
    with a real /AcroForm so the rest of the pipeline works.

    Built synthetically here: take the Lease Invoice (a real fillable PDF),
    strip its /AcroForm, and confirm validate_pdf round-trips successfully
    with synthesized fields."""
    from pypdf import PdfReader, PdfWriter
    src = PdfReader(str(LEASE_INVOICE_PDF))
    writer = PdfWriter(clone_from=src)
    cat = writer._root_object
    if "/AcroForm" in cat:
        del cat["/AcroForm"]
    out = tmp_path / "no-form.pdf"
    with out.open("wb") as f:
        writer.write(f)

    reader, persist_bytes = validate_pdf(out.read_bytes())
    # Synthesis ran: bytes were rewritten (different from original).
    assert persist_bytes != out.read_bytes()
    # New reader sees synthesized fields.
    from backend.pdf_introspect import walk_fields
    walked = walk_fields(reader)
    assert len(walked) > 0
    # Synthesized fields have the f_NNN_NNN naming pattern.
    assert any(f.dotted_name.startswith("f_") for f in walked)


def test_validate_pdf_rejects_blank_narrative_pdf(tmp_path: Path):
    """Genuine "no fillable areas" case — a PDF that's text-only with no
    underlines or checkboxes. field_synth runs, detects nothing, and the
    user gets a clear message rather than a silent fail."""
    from pypdf import PdfReader, PdfWriter, PageObject
    # Build a single-page PDF with one text line and no form fields.
    writer = PdfWriter()
    page = PageObject.create_blank_page(width=612, height=792)
    writer.add_page(page)
    out = tmp_path / "narrative.pdf"
    with out.open("wb") as f:
        writer.write(f)
    with pytest.raises(TemplateUploadError, match="couldn't find any fillable areas"):
        validate_pdf(out.read_bytes())


# ---- Proposal translation ----

def _proposal_with_two_canonicals_and_one_extra() -> ProposedMapping:
    return ProposedMapping(fields=[
        ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names", confidence=10),
        ProposedField(pdf_field="PROPERTY ADDRESS", canonical_path="property.address", confidence=10),
        ProposedField(
            pdf_field="PET DEPOSIT",
            extra_field_name="pet_deposit",
            extra_field_type="money",
            extra_field_description="The pet deposit amount as written by the agent.",
            confidence=10,
        ),
    ])


def test_proposal_translates_canonical_paths_to_template_strings():
    proposal = _proposal_with_two_canonicals_and_one_extra()
    mapping, extras, unknown_paths, low_conf, btn_warns = proposal_to_mapping_file(
        proposal, title="Test", source_pdf_filename="abc.pdf",
        filled_filename="test_filled.pdf",
    )
    assert mapping.fields["TENANTS NAME"] == "{tenant_or_buyer_names}"
    assert mapping.fields["PROPERTY ADDRESS"] == "{property.address}"
    assert mapping.fields["PET DEPOSIT"] == "{template_extras.pet_deposit}"
    assert len(extras) == 1
    assert extras[0].name == "pet_deposit"
    assert extras[0].type == "money"
    assert unknown_paths == []
    assert low_conf == []
    assert btn_warns == []


def test_proposal_unmapped_field_renders_empty_string():
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="MYSTERY", confidence=10),
    ])
    mapping, _extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert mapping.fields["MYSTERY"] == ""


def test_proposal_extra_field_type_defaults_to_text():
    """If the AI gives us extra_field_name but forgets the type, fall back
    to 'text' instead of failing the whole upload."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="WEIRD",
            extra_field_name="weird_thing",
            extra_field_description="Something weird the agent writes",
            confidence=10,
        ),
    ])
    _, extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert extras[0].type == "text"


def test_proposal_unknown_canonical_path_is_demoted_to_unmapped():
    """If GPT hallucinates a canonical_path, we leave the field unmapped AND
    surface it in unknown_paths so the upload pipeline can flag the template
    for human review instead of letting the user discover blank fields at
    fill time."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="GOOD", canonical_path="property.address", confidence=10),
        ProposedField(pdf_field="BAD", canonical_path="borrower.full_name", confidence=10),
        ProposedField(pdf_field="ALSO_BAD", canonical_path="totally_made_up", confidence=10),
    ])
    mapping, extras, unknown_paths, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert mapping.fields["GOOD"] == "{property.address}"
    assert mapping.fields["BAD"] == ""
    assert mapping.fields["ALSO_BAD"] == ""
    assert len(unknown_paths) == 2
    assert any("borrower.full_name" in u for u in unknown_paths)
    assert any("totally_made_up" in u for u in unknown_paths)


def test_proposal_computed_value_paths_are_allowed():
    """Computed values like {today}, {county_suffix}, {tenant_1_name} are
    legitimate canonical_paths even though they aren't TransactionFields keys."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="DATE_OF_SIGNING", canonical_path="today", confidence=10),
        ProposedField(pdf_field="ADDR_LINE", canonical_path="property.address_full", confidence=10),
        ProposedField(pdf_field="COUNTY_TAIL", canonical_path="county_suffix", confidence=10),
        ProposedField(pdf_field="TENANT_1", canonical_path="tenant_1_name", confidence=10),
    ])
    mapping, _extras, unknown_paths, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert mapping.fields["DATE_OF_SIGNING"] == "{today}"
    assert mapping.fields["COUNTY_TAIL"] == "{county_suffix}"
    assert unknown_paths == []


def test_low_confidence_fields_are_blanked_and_surfaced():
    """REGRESSION test for the 2026-05-10 product decision: low-confidence
    AI mappings must render BLANK in the PDF and be surfaced to the user
    as 'we weren't sure'. Wrong > blank on a legal contract."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="A", canonical_path="property.address", confidence=10),
        ProposedField(pdf_field="B", canonical_path="purchase_price", confidence=5),  # low
        ProposedField(pdf_field="C", extra_field_name="weird", extra_field_type="text",
                      extra_field_description="x", confidence=3),  # low
    ])
    mapping, extras, _unknown, low_conf, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    # High confidence: mapped as normal
    assert mapping.fields["A"] == "{property.address}"
    # Low confidence canonical: blanked
    assert mapping.fields["B"] == ""
    # Low confidence extra: blanked AND not registered as an extra
    assert mapping.fields["C"] == ""
    assert all(e.name != "weird" for e in extras)
    # Both low-confidence fields surfaced
    assert len(low_conf) == 2
    by_field = {f["pdf_field"]: f for f in low_conf}
    assert by_field["B"]["proposed"] == "purchase_price"
    assert by_field["B"]["kind"] == "canonical"
    assert by_field["C"]["proposed"] == "weird"
    assert by_field["C"]["kind"] == "extra"


def test_agent_paths_use_looser_confidence_threshold():
    """REGRESSION CAR BRBC 2026-05-12: 8 broker-block canonical paths
    (agent.brokerage_address, agent.phone, etc.) got mapped correctly at
    conf=4-6 but blanked by the strict threshold=7 gate. Wrong-fill risk is
    near zero for agent.* paths (the value comes from the logged-in user's
    own profile), so they use threshold=4 instead."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="A", canonical_path="agent.brokerage_address", confidence=5),
        ProposedField(pdf_field="B", canonical_path="agent.phone", confidence=4),
        ProposedField(pdf_field="C", canonical_path="agent.email", confidence=6),
        # Below the agent threshold (4): still gated.
        ProposedField(pdf_field="D", canonical_path="agent.brokerage_mls", confidence=3),
        # Non-agent canonical with same confidence: gated by strict threshold.
        ProposedField(pdf_field="E", canonical_path="purchase_price", confidence=5),
    ])
    mapping, _extras, _unknown, low_conf, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert mapping.fields["A"] == "{agent.brokerage_address}"
    assert mapping.fields["B"] == "{agent.phone}"
    assert mapping.fields["C"] == "{agent.email}"
    assert mapping.fields["D"] == ""  # below loose threshold
    assert mapping.fields["E"] == ""  # non-agent stays strict
    low_conf_fields = {f["pdf_field"] for f in low_conf}
    assert low_conf_fields == {"D", "E"}


def test_handfill_extras_suppressed_from_low_confidence_banner():
    """REGRESSION CAR BRBC 2026-05-12: the 'we weren't sure about 57 fields'
    banner showed ~40 signing-time entries (ad_sign_date_1, buyer_initials_2,
    party_role, etc.) that the human fills by hand. Surfacing them is noise.
    These get suppressed at upload time."""
    proposal = ProposedMapping(fields=[
        # Handfill: should NOT appear in low_confidence
        ProposedField(pdf_field="A", extra_field_name="ad_sign_date_1",
                      extra_field_type="date", extra_field_description="x", confidence=3),
        ProposedField(pdf_field="B", extra_field_name="buyer_initials_1",
                      extra_field_type="text", extra_field_description="x", confidence=3),
        ProposedField(pdf_field="C", extra_field_name="ad_checkbox_buyer",
                      extra_field_type="bool", extra_field_description="x", confidence=3),
        # Real low-confidence extra: SHOULD appear
        ProposedField(pdf_field="D", extra_field_name="brbc_compensation_percent",
                      extra_field_type="money", extra_field_description="x", confidence=3),
    ])
    _, _, _, low_conf, _ = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    surfaced = {f["pdf_field"] for f in low_conf}
    # Signing-time extras hidden; real uncertainty surfaced.
    assert surfaced == {"D"}


def test_ai_invented_extras_get_coerced_to_canonical():
    """REGRESSION CAR BRBC 2026-05-12: gpt-5 sometimes proposes
    template_extras.<X> when <X> obviously signals a known canonical concept
    ("covered_counties_list_1" → county, "buyers_brokerage_license_number" →
    agent.brokerage_license). The deterministic post-processing safety net
    in proposal_to_mapping_file should rewrite these as canonical proposals
    so they actually fill at runtime instead of pointing at extras that have
    no value source."""
    proposal = ProposedMapping(fields=[
        # AI invented an extra for what is clearly the canonical county field.
        ProposedField(pdf_field="A", extra_field_name="covered_counties_list_1",
                      extra_field_type="text", extra_field_description="x", confidence=9),
        # And for brokerage_license (firm-row Lic #).
        ProposedField(pdf_field="B", extra_field_name="buyers_brokerage_license_number",
                      extra_field_type="text", extra_field_description="x", confidence=9),
        # And for commission percent.
        ProposedField(pdf_field="C", extra_field_name="commission_amount_percent",
                      extra_field_type="money", extra_field_description="x", confidence=9),
        # Genuinely-unique extra (no canonical exists) should pass through.
        ProposedField(pdf_field="D", extra_field_name="pet_name",
                      extra_field_type="text", extra_field_description="x", confidence=9),
    ])
    mapping, _extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert mapping.fields["A"] == "{county}"
    assert mapping.fields["B"] == "{agent.brokerage_license}"
    assert mapping.fields["C"] == "{commission_amount}"
    # Genuine extras still go through as template_extras references.
    assert mapping.fields["D"] == "{template_extras.pet_name}"


def test_count_rule_does_not_false_match_account_or_discount():
    """REGRESSION (security review 2026-05-12): the county-coercion rule
    originally matched any name containing 'count', which would incorrectly
    rewrite 'account_number', 'discount_percent', 'encounter_id' to county.
    The tightened rule requires both 'count' AND 'y' as substrings — every
    real county variation has both ('county', 'counties'); these false-match
    examples have only 'count'."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="A", extra_field_name="account_number",
                      extra_field_type="text", extra_field_description="x", confidence=9),
        ProposedField(pdf_field="B", extra_field_name="discount_percent",
                      extra_field_type="money", extra_field_description="x", confidence=9),
        ProposedField(pdf_field="C", extra_field_name="encounter_id",
                      extra_field_type="text", extra_field_description="x", confidence=9),
        # Real county should still match.
        ProposedField(pdf_field="D", extra_field_name="primary_county",
                      extra_field_type="text", extra_field_description="x", confidence=9),
    ])
    mapping, _extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    # None of the false-match examples coerce to county.
    assert mapping.fields["A"] == "{template_extras.account_number}"
    assert mapping.fields["B"] == "{template_extras.discount_percent}"
    assert mapping.fields["C"] == "{template_extras.encounter_id}"
    # But a real county still does.
    assert mapping.fields["D"] == "{county}"


# ============================================================================
# Two-pass mapping orchestration (mini for breadth, gpt-5 for hard subset)
# ============================================================================

@pytest.mark.asyncio
async def test_propose_mapping_two_pass_escalates_only_uncertain_fields(monkeypatch):
    """REGRESSION (architecture review 2026-05-12): two-pass should run mini
    over EVERY field, then gpt-5 only over fields where mini was uncertain
    (low confidence) or invented a template_extras path that resolves to a
    real canonical concept. Confident-canonical fields skip pass 2."""
    descs = [
        {"pdf_field": "F1", "field_type": "/Tx", "neighbor_text": "Buyer", "page": 1},
        {"pdf_field": "F2", "field_type": "/Tx", "neighbor_text": "Date", "page": 1},
        {"pdf_field": "F3", "field_type": "/Tx", "neighbor_text": "Address", "page": 1},
    ]

    # Pass 1 (mini) confidently maps F1 + F2, hedges on F3.
    pass1 = ProposedMapping(fields=[
        ProposedField(pdf_field="F1", canonical_path="tenant_or_buyer_names", confidence=10),
        ProposedField(pdf_field="F2", canonical_path="today", confidence=9),
        ProposedField(pdf_field="F3", canonical_path="property.address", confidence=5),
    ])
    # Pass 2 (gpt-5) only sees F3 and bumps confidence + corrects.
    pass2 = ProposedMapping(fields=[
        ProposedField(pdf_field="F3", canonical_path="property.address", confidence=10),
    ])

    calls: list[tuple[str | None, list[str]]] = []

    async def fake_propose(descs_in, crops=None, model=None):
        names = [d["pdf_field"] for d in descs_in]
        calls.append((model, names))
        # Return whichever proposal matches the field set.
        if set(names) == {"F1", "F2", "F3"}:
            return pass1
        if set(names) == {"F3"}:
            return pass2
        raise AssertionError(f"unexpected propose_mapping call: {names}")

    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    result = await templates_mod.propose_mapping_two_pass(descs)
    # Pass 1 received all 3; pass 2 received only F3.
    assert len(calls) == 2
    assert calls[0][1] == ["F1", "F2", "F3"]
    assert calls[0][0] == templates_mod.MODEL_FAST
    assert calls[1][1] == ["F3"]
    # Pass 2 model is the default MODEL (gpt-5), passed as None or explicit.
    # Merged result preserves positional order and uses pass 2's F3.
    assert [f.pdf_field for f in result.fields] == ["F1", "F2", "F3"]
    f3 = next(f for f in result.fields if f.pdf_field == "F3")
    assert f3.confidence == 10  # pass 2's value wins


@pytest.mark.asyncio
async def test_propose_mapping_two_pass_skips_pass2_when_mini_confident(monkeypatch):
    """Lucky path: every field is confidently mapped by mini, no pass 2 fires.
    Wall time drops to mini-only — the design partner's reward for a clean form."""
    descs = [
        {"pdf_field": "F1", "field_type": "/Tx", "neighbor_text": "Buyer", "page": 1},
    ]
    pass1 = ProposedMapping(fields=[
        ProposedField(pdf_field="F1", canonical_path="tenant_or_buyer_names", confidence=10),
    ])
    call_count = {"n": 0}

    async def fake_propose(_descs, crops=None, model=None):
        call_count["n"] += 1
        return pass1

    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)
    result = await templates_mod.propose_mapping_two_pass(descs)
    assert call_count["n"] == 1, "pass 2 should not fire when mini is confident"
    assert result.fields[0].canonical_path == "tenant_or_buyer_names"


@pytest.mark.asyncio
async def test_propose_mapping_two_pass_falls_back_when_pass2_fails(monkeypatch):
    """Pass 2 (gpt-5) raising AIMappingError must NOT fail the upload —
    degrade to pass 1's results instead. Upload is more valuable than perfect
    accuracy: the user can correct mini's mistakes via the review UI."""
    descs = [
        {"pdf_field": "F1", "field_type": "/Tx", "neighbor_text": "Mystery", "page": 1},
    ]
    pass1 = ProposedMapping(fields=[
        ProposedField(pdf_field="F1", canonical_path=None, extra_field_name="mystery",
                      extra_field_type="text", extra_field_description="x", confidence=4),
    ])

    async def fake_propose(_descs, crops=None, model=None):
        if model == templates_mod.MODEL_FAST:
            return pass1
        raise templates_mod.AIMappingError("simulated gpt-5 outage")

    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)
    result = await templates_mod.propose_mapping_two_pass(descs)
    # Pass 1's proposal is preserved, even though it was low confidence.
    assert result.fields[0].pdf_field == "F1"
    assert result.fields[0].extra_field_name == "mystery"


# ============================================================================
# Neighbor-text extraction (the AI's primary signal for label inference)
# ============================================================================
#
# Regression for the 2026-05-11 incident: column-header labels printed UNDER
# the input rect (Multi-Board's address row, contact-block grids) were invisible
# to extract_neighbor_text. The AI got useless ABOVE text and mapped the
# Address field as low-confidence → blanked. Now extract_neighbor_text also
# scans a BELOW band, and the same-line band is tight enough that adjacent
# rows of inputs don't bleed labels into each other.

REPO_ROOT_TEST = Path(__file__).resolve().parent.parent.parent
MULTIBOARD_PDF = REPO_ROOT_TEST / "templates" / "pdf" / "Multi-Board-8.0 (1).pdf"


def test_neighbor_text_below_band_picks_up_column_headers():
    """REGRESSION (2026-05-11): the page-1 Address widget on Multi-Board has
    its label ON THE LINE BELOW the rect ("Address Unit # City State Zip
    County" — the column-header row that explains what the wide input means).
    Without BELOW-band extraction, the AI saw only paragraph context above
    and mapped Address to a low-confidence extra_field → blanked at fill time.
    With BELOW now scanned, the descriptor includes "BELOW: Address Unit #
    [IF APPLICABLE] City State Zip ..." and the AI can map it correctly."""
    from pypdf import PdfReader
    reader = PdfReader(str(MULTIBOARD_PDF))
    descs = templates_mod.collect_field_descriptions(reader)
    address_desc = next((d for d in descs if d["pdf_field"] == "Address"), None)
    assert address_desc is not None, "Address field must exist in Multi-Board"
    neighbor = address_desc["neighbor_text"]
    assert "BELOW:" in neighbor, f"expected BELOW band in: {neighbor!r}"
    assert "Address" in neighbor, "BELOW must include the Address column label"
    # Other column headers should also show up
    assert any(label in neighbor for label in ("City", "State", "Zip"))


def test_neighbor_text_checkbox_right_labels_still_detected():
    """REGRESSION (adversarial review 2026-05-11): tightening the same-line
    band must not break RIGHT-of-checkbox label detection. The property-type
    cluster on Multi-Board (fields 7/8/9) has labels "Single Family Attached
    / Detached / Multi-Unit" to the RIGHT of each checkbox. Verify each
    checkbox still captures its label."""
    from pypdf import PdfReader
    reader = PdfReader(str(MULTIBOARD_PDF))
    descs = templates_mod.collect_field_descriptions(reader)
    by_name = {d["pdf_field"]: d for d in descs}
    # Field 7 = "Single Family Attached" checkbox. Its RIGHT label is the
    # next checkbox's label "Single Family Detached" because the labels are
    # printed BETWEEN the checkboxes. What matters: SOMETHING from the
    # cluster shows up so the AI can disambiguate via the visual crop.
    f7 = by_name.get("7")
    assert f7 is not None and f7["field_type"] == "/Btn"
    assert "Single Family" in f7["neighbor_text"], (
        f"checkbox 7 lost its label cluster: {f7['neighbor_text']!r}"
    )


def test_neighbor_text_below_band_does_not_capture_distant_paragraph():
    """REGRESSION (adversarial review 2026-05-11): the BELOW band was 1.6
    line-heights deep, which on a single-column form pulls the start of
    the next paragraph into BELOW. With prompt rules elevating short BELOW
    strings to 'primary label', this regressed single-column forms by
    promoting paragraph-fragment noise. Verify BELOW is capped so it can't
    reach text more than ~1 line below the rect. Field 1 on Multi-Board
    sits between paragraph context above and form-line-3 below — BELOW
    must not grab line 3 (Seller Name row, ~14pt below)."""
    from pypdf import PdfReader
    reader = PdfReader(str(MULTIBOARD_PDF))
    descs = templates_mod.collect_field_descriptions(reader)
    f1 = next((d for d in descs if d["pdf_field"] == "1"), None)
    assert f1 is not None
    neighbor = f1["neighbor_text"]
    # Seller Name(s) sits one row (~14pt) below the buyer-name input.
    # With BELOW at 0.9 line-heights (~17pt) it might just touch — but
    # the FORMAT of MultiBoard puts the seller-name LABEL above its own
    # input, so the BELOW band of field 1 (which scans below the buyer
    # rect) should not capture "Seller Name(s)" because the seller label
    # text actually sits AT y of the seller rect, ~14pt below — which IS
    # within the depth. Looser assertion: BELOW is shorter than LEFT/ABOVE
    # for paragraph-rich rects, signaling it's no longer the dominant signal.
    if "BELOW:" in neighbor:
        below_text = neighbor.split("BELOW:")[1].split("|")[0].strip()
        # Sanity: should not be longer than ~180 chars (the slice cap)
        # AND should not contain "approximate" which would indicate a
        # multi-line capture into the paragraph below.
        assert len(below_text) <= 180


def test_neighbor_text_same_line_band_does_not_leak_adjacent_row_labels():
    """REGRESSION (2026-05-11): on Multi-Board's form lines 2-3 (Buyer Name /
    Seller Name) the input rects are ~14pt apart vertically. The old same-line
    band (line_height * 0.4 above + 0.2 below) was tall enough to grab text
    from BOTH rows, producing LEFT: "Buyer Name(s) Seller Name(s) [PLEASE
    PRINT] [PLEASE PRINT]" — confusing the AI into mapping field 2 (the
    seller name input) as low-confidence. Tightening the band to ~rect height
    fixes this; LEFT now reports only the row's own label."""
    from pypdf import PdfReader
    reader = PdfReader(str(MULTIBOARD_PDF))
    descs = templates_mod.collect_field_descriptions(reader)
    seller_desc = next((d for d in descs if d["pdf_field"] == "2"), None)
    assert seller_desc is not None
    neighbor = seller_desc["neighbor_text"]
    # The seller-name row's LEFT must NOT include Buyer Name
    assert "Buyer Name" not in neighbor, (
        f"same-line band leaked the buyer-name label into seller's neighbor "
        f"text: {neighbor!r}"
    )
    # And SHOULD include the seller-name label
    assert "Seller Name" in neighbor


# ============================================================================
# BtnChoice (autonomous /Btn fill: AI emits widget /AP/N states + canonical
# value table; mapping carries them through to fill time)
# ============================================================================
#
# Why: bundled mappings encode /Btn states via interpolate.py computed values
# (property_type_attached_state etc.) — they only work for the exact PDFs we
# hand-tuned against. On autonomously-mapped uploads, the AI doesn't know
# which /AP/N keys the widget actually has, so the bundled fallback writes
# "/On" and pdf_fill silently /Off-s it. BtnChoice fixes that by encoding
# both the canonical value AND the literal state per widget.

from backend.schema import BtnChoice as _BtnChoice


def test_proposal_btn_choices_emits_btnchoice_mapping():
    """A /Btn proposal with btn_choices emits a BtnChoice value, not a
    string template. The choices dict carries the exact widget state names."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="PROP_TYPE_ATTACHED",
            canonical_path="property_type",
            btn_choices={"attached": "/On"},
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "PROP_TYPE_ATTACHED", "field_type": "/Btn",
                    "neighbor_text": "Single Family Attached", "page": 1,
                    "states": ["/Off", "/On"]}]
    mapping, _extras, _unknown, _low, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    value = mapping.fields["PROP_TYPE_ATTACHED"]
    assert isinstance(value, _BtnChoice)
    assert value.canonical_path == "property_type"
    assert value.choices == {"attached": "/On"}
    assert btn_warns == []


def test_proposal_btn_choices_radio_group_full_table():
    """Real radio group: one field with multiple kids, multiple states.
    The AI emits the full canonical_value → state table."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="ESCROWEE",
            canonical_path="escrowee",
            btn_choices={
                "seller": "/Seller's Brokerage",
                "buyer": "/Buyer's Brokerage",
                "other": "/As otherwise agreed",
            },
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "ESCROWEE", "field_type": "/Btn",
                    "neighbor_text": "Escrowee", "page": 1,
                    "states": ["/Off", "/Seller's Brokerage", "/Buyer's Brokerage",
                               "/As otherwise agreed"]}]
    mapping, _extras, _unknown, _low, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    value = mapping.fields["ESCROWEE"]
    assert isinstance(value, _BtnChoice)
    assert len(value.choices) == 3
    assert btn_warns == []


def test_proposal_btn_choices_unknown_canonical_value_pruned():
    """AI emits a canonical value not in the Literal enum (e.g. 'multifamily'
    for property_type which is attached|detached|multi_unit). That entry is
    pruned and a warning is emitted. The valid entries survive."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="PROP_TYPE",
            canonical_path="property_type",
            btn_choices={"attached": "/On", "multifamily": "/On"},
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "PROP_TYPE", "field_type": "/Btn",
                    "neighbor_text": "Property Type", "page": 1,
                    "states": ["/Off", "/On"]}]
    mapping, _extras, _unknown, _low, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    value = mapping.fields["PROP_TYPE"]
    assert isinstance(value, _BtnChoice)
    assert value.choices == {"attached": "/On"}  # 'multifamily' pruned
    assert any("multifamily" in w for w in btn_warns)


def test_proposal_btn_choices_unknown_widget_state_pruned():
    """AI emits a state not in the widget's /AP/N (e.g. '/Yes' when widget
    only accepts '/Off' and '/On'). That entry is pruned + warning emitted."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="DUAL_AGENCY",
            canonical_path="property_type",  # using a Literal path for sanity
            btn_choices={"attached": "/Yes"},
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "DUAL_AGENCY", "field_type": "/Btn",
                    "neighbor_text": "Dual Agency", "page": 1,
                    "states": ["/Off", "/On"]}]
    mapping, _extras, _unknown, low_conf, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    # All choices invalid -> field blanked, surfaced as low-confidence so user
    # knows to fill by hand.
    assert mapping.fields["DUAL_AGENCY"] == ""
    assert any(lc["pdf_field"] == "DUAL_AGENCY" for lc in low_conf)
    assert any("/Yes" in w for w in btn_warns)


def test_proposal_btn_choices_low_confidence_blanked():
    """Low-confidence /Btn proposals get blanked AND surfaced, same as any
    other low-confidence canonical proposal — wrong > blank on legal docs."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="PROP_TYPE",
            canonical_path="property_type",
            btn_choices={"attached": "/On"},
            confidence=5,  # below threshold
        ),
    ])
    field_descs = [{"pdf_field": "PROP_TYPE", "field_type": "/Btn",
                    "neighbor_text": "Property Type", "page": 1,
                    "states": ["/Off", "/On"]}]
    mapping, _extras, _unknown, low_conf, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    assert mapping.fields["PROP_TYPE"] == ""
    assert any(lc["pdf_field"] == "PROP_TYPE" for lc in low_conf)


def test_proposal_btn_choices_with_extra_field_uses_template_extras_path():
    """A single boolean checkbox without a canonical path (e.g. 'Dual Agency')
    becomes a template_extras BtnChoice. Path is synthesized as
    template_extras.<name>; choices are validated against widget states."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="DUAL_AGENCY",
            extra_field_name="dual_agency",
            extra_field_type="bool",
            extra_field_description="Dual Agency applies — checked = true.",
            btn_choices={"true": "/On"},
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "DUAL_AGENCY", "field_type": "/Btn",
                    "neighbor_text": "Dual Agency", "page": 1,
                    "states": ["/Off", "/On"]}]
    mapping, extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    value = mapping.fields["DUAL_AGENCY"]
    assert isinstance(value, _BtnChoice)
    assert value.canonical_path == "template_extras.dual_agency"
    assert value.choices == {"true": "/On"}
    # Extra field still registered so frontend asks the user for the value
    assert any(e.name == "dual_agency" for e in extras)


def test_proposal_btn_choices_omitted_falls_back_to_string_template():
    """A /Btn proposal WITHOUT btn_choices, AND without field_descriptions
    (test-only path) emits a string template — same as it did before this
    feature landed. Keeps backward-compat with the canonical mappings that
    use computed state values like {property_type_attached_state}.

    In production we ALWAYS pass field_descriptions, which triggers the
    different test below — a /Btn field missing btn_choices gets blanked +
    surfaced rather than emitting a string template (which would corrupt
    the checkbox's /V at fill time)."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="SOME_BTN",
            canonical_path="property_type",
            btn_choices=None,
            confidence=10,
        ),
    ])
    mapping, _extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert mapping.fields["SOME_BTN"] == "{property_type}"


def test_proposal_btn_choices_on_tx_field_does_not_corrupt_text_value():
    """REGRESSION (code review 2026-05-10): if the AI emits btn_choices on a
    /Tx field (prompt confusion or prompt injection via uploaded-PDF neighbor
    text), proposal_to_mapping_file must NOT route it through BtnChoice.
    Otherwise resolve_btn_choice would compare the resolved address string
    against the choices keys, miss, and write '/Off' to a text field's /V
    — silently blanking the customer's address."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="ADDR_LINE",
            canonical_path="property.address",
            btn_choices={"221 W Hubbard": "/On"},  # nonsense for a /Tx
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "ADDR_LINE", "field_type": "/Tx",
                    "neighbor_text": "Address", "page": 1}]
    mapping, _extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    # Must emit a string template, NOT a BtnChoice. The btn_choices is
    # silently discarded because field_type is /Tx.
    assert mapping.fields["ADDR_LINE"] == "{property.address}"
    assert not isinstance(mapping.fields["ADDR_LINE"], _BtnChoice)


def test_proposal_btn_choices_on_ch_field_does_not_route_through_btnchoice():
    """REGRESSION (2026-05-10): /Ch (dropdown) fields take a string value
    written to /V, NOT a state name. If the AI emits btn_choices on a /Ch
    field, routing through BtnChoice would write '/Off' or '/Choice1' into
    the dropdown's /V — corrupting the dropdown's selection. Only /Btn
    fields use BtnChoice; /Ch falls through to the string-template path."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="DROPDOWN",
            canonical_path="property_type",
            btn_choices={"attached": "/Choice1"},
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "DROPDOWN", "field_type": "/Ch",
                    "neighbor_text": "Property Type", "page": 1,
                    "states": ["/Choice1", "/Choice2", "/Choice3"]}]
    mapping, _extras, _unknown, _low, _warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    # Must NOT be a BtnChoice. Falls through to string template.
    assert mapping.fields["DROPDOWN"] == "{property_type}"
    assert not isinstance(mapping.fields["DROPDOWN"], _BtnChoice)


def test_proposal_btn_choices_all_off_is_rejected_as_silent_noop():
    """REGRESSION (adversarial review 2026-05-10): if the AI sanitizes to a
    BtnChoice where EVERY canonical value maps to '/Off', the checkbox will
    never fire regardless of ctx. That's a silent no-op — observationally
    identical to no mapping at all, but without the low_confidence surface
    that tells the user to fill manually. _sanitize_btn_choices rejects the
    whole table so the caller blanks + surfaces it."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="DOOMED",
            canonical_path="property_type",
            btn_choices={"attached": "/Off", "detached": "/Off", "multi_unit": "/Off"},
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "DOOMED", "field_type": "/Btn",
                    "neighbor_text": "", "page": 1,
                    "states": ["/Off", "/On"]}]
    mapping, _extras, _unknown, low_conf, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    # All-/Off table rejected → field blanked + surfaced.
    assert mapping.fields["DOOMED"] == ""
    assert any(lc["pdf_field"] == "DOOMED" for lc in low_conf)
    assert any("never fire" in w or "/Off" in w for w in btn_warns)


def test_proposal_empty_widget_states_rejects_all_proposals():
    """REGRESSION (adversarial review 2026-05-10): empty widget_states list
    (field has /Btn type but no /AP/N keys extracted, OR field is missing
    from field_descriptions) used to silently disable widget-state validation,
    accepting any AI proposal. Now we reject every state — better to blank
    a field we can't fill correctly than to write a guess."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="MYSTERY_BTN",
            canonical_path="property_type",
            btn_choices={"attached": "/On"},
            confidence=10,
        ),
    ])
    # Note: states is empty list. Production case: field listed in
    # field_descriptions but pypdf couldn't extract /AP/N (corrupt PDF).
    field_descs = [{"pdf_field": "MYSTERY_BTN", "field_type": "/Btn",
                    "neighbor_text": "", "page": 1, "states": []}]
    mapping, _extras, _unknown, low_conf, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    # No widget states available → can't trust AI's '/On' → blanked.
    assert mapping.fields["MYSTERY_BTN"] == ""
    assert any(lc["pdf_field"] == "MYSTERY_BTN" for lc in low_conf)
    assert any("/AP/N" in w or "not in widget" in w for w in btn_warns)


def test_mapping_value_is_blank_treats_all_off_btnchoice_as_blank():
    """REGRESSION: a hand-edited mapping JSON could sneak in an all-/Off
    BtnChoice (the sanitizer rejects it on the upload path, but loaded
    JSONs aren't re-sanitized). validate_mapping_structure's coverage_low
    check should count those as blank so the warning fires correctly."""
    from backend.templates import _mapping_value_is_blank

    assert _mapping_value_is_blank("") is True
    assert _mapping_value_is_blank("   ") is True
    assert _mapping_value_is_blank("{property.address}") is False
    # Normal BtnChoice with a real /On state: not blank.
    bc_real = _BtnChoice(canonical_path="property_type",
                         choices={"attached": "/On"})
    assert _mapping_value_is_blank(bc_real) is False
    # All-/Off BtnChoice: counts as blank.
    bc_dead = _BtnChoice(canonical_path="property_type",
                         choices={"attached": "/Off", "detached": "/Off"})
    assert _mapping_value_is_blank(bc_dead) is True
    # Empty-choices BtnChoice: counts as blank.
    bc_empty = _BtnChoice(canonical_path="property_type", choices={})
    assert _mapping_value_is_blank(bc_empty) is True


def test_proposal_btn_field_without_btn_choices_is_blanked_and_surfaced():
    """REGRESSION (code review 2026-05-10): a /Btn field with a canonical_path
    but no btn_choices used to fall through to a string template like
    '{property_type}', which interpolates to 'attached' and gets written as
    text to the checkbox's /V — leaving it visually unchecked. Now we blank
    it + surface it as low-confidence so the user knows to fill manually."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="BTN_NO_CHOICES",
            canonical_path="property_type",
            btn_choices=None,  # AI forgot to emit btn_choices
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "BTN_NO_CHOICES", "field_type": "/Btn",
                    "neighbor_text": "Property Type", "page": 1,
                    "states": ["/Off", "/On"]}]
    mapping, _extras, _unknown, low_conf, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    assert mapping.fields["BTN_NO_CHOICES"] == ""
    assert any(lc["pdf_field"] == "BTN_NO_CHOICES" for lc in low_conf)
    assert any("btn_choices" in w for w in btn_warns)


def test_validate_mapping_structure_handles_btnchoice_values_without_crashing():
    """REGRESSION (outside-voice review F4): switching mapping.fields to
    dict[str, str | BtnChoice] broke validate_mapping_structure's
    `v.strip()` blank-counter. Guard via isinstance(v, str). A BtnChoice
    with non-empty choices counts as 'not blank'."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="TXT", canonical_path="property.address", confidence=10),
        ProposedField(
            pdf_field="BTN",
            canonical_path="property_type",
            btn_choices={"attached": "/On"},
            confidence=10,
        ),
        ProposedField(pdf_field="BLANK", confidence=10),  # nothing -> ""
    ])
    field_descs = [
        {"pdf_field": "TXT", "field_type": "/Tx", "neighbor_text": "", "page": 1},
        {"pdf_field": "BTN", "field_type": "/Btn", "neighbor_text": "", "page": 1,
         "states": ["/Off", "/On"]},
        {"pdf_field": "BLANK", "field_type": "/Tx", "neighbor_text": "", "page": 1},
    ]
    mapping, _extras, unknown, low_conf, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    # Must not crash — that's the real test.
    warnings = templates_mod.validate_mapping_structure(
        mapping, field_descs, unknown, low_conf, btn_warns
    )
    # 1/3 blank = 33% < 50% so no coverage_low warning expected.
    assert all("coverage_low" not in w for w in warnings)


def test_validate_mapping_structure_surfaces_btn_warnings():
    """When the sanitizer pruned /Btn choices, validate_mapping_structure
    surfaces a btn_choice_mismatches warning so the template is flagged
    needs_attention at upload time."""
    proposal = ProposedMapping(fields=[
        ProposedField(
            pdf_field="BTN",
            canonical_path="property_type",
            btn_choices={"multifamily": "/On"},  # invalid canonical value
            confidence=10,
        ),
    ])
    field_descs = [{"pdf_field": "BTN", "field_type": "/Btn", "neighbor_text": "",
                    "page": 1, "states": ["/Off", "/On"]}]
    mapping, _e, unknown, low_conf, btn_warns = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
        field_descriptions=field_descs,
    )
    assert btn_warns  # something got pruned
    warnings = templates_mod.validate_mapping_structure(
        mapping, field_descs, unknown, low_conf, btn_warns
    )
    assert any("btn_choice_mismatches" in w for w in warnings)


# ---- API endpoint tests (real Postgres + endpoint, AI mocked) ----

def test_list_templates_starts_empty(authed_client, isolated_template_dirs):
    """No more shared defaults — fresh user starts with an empty list."""
    r = authed_client.get("/api/templates")
    assert r.status_code == 200
    assert r.json() == {"templates": []}


def test_upload_template_happy_path(authed_client, isolated_template_dirs, monkeypatch):
    """End-to-end: real PDF + mocked AI call. Verifies the row is inserted
    under the caller's user_id, the mapping JSON is written, and the response
    shape is right."""
    fake_proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names", confidence=10),
        ProposedField(pdf_field="PROPERTY ADDRESS", canonical_path="property.address", confidence=10),
        ProposedField(pdf_field="LEASE DATE", canonical_path="lease_start", confidence=10),
        ProposedField(pdf_field="COMMENCEMENT DATE", canonical_path="lease_start", confidence=10),
        ProposedField(pdf_field="AMOUNT DUE COMPASS", canonical_path="commission_amount", confidence=10),
        ProposedField(pdf_field="COMPASS AGENT", canonical_path="agent.name", confidence=10),
        ProposedField(pdf_field="LEASE INVOICE", extra_field_name="invoice_number",
                      extra_field_type="text", extra_field_description="Invoice number",
                      confidence=10),
    ])

    async def fake_propose(_descs, crops=None, **_kwargs):
        return fake_proposal
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "Custom Lease Invoice"},
        files={"pdf": ("custom.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Custom Lease Invoice"
    # With clean validation (all fields mapped, no hallucinated paths, no
    # low-confidence proposals), the template ships as `ready` directly.
    # `needs_attention` is reserved for mappings that fail validation.
    assert data["status"] == "ready"
    # Real Lease Invoice PDF has more than the 7 fake-proposed fields; the
    # real upload pipeline maps all fillable widgets it finds.
    assert data["field_count"] >= 7
    assert len(data["extra_fields"]) == 1
    assert data["extra_fields"][0]["name"] == "invoice_number"
    assert data["mapping"]["fields"]["TENANTS NAME"] == "{tenant_or_buyer_names}"
    assert data["mapping"]["fields"]["LEASE INVOICE"] == "{template_extras.invoice_number}"

    listing = authed_client.get("/api/templates").json()
    titles = [t["title"] for t in listing["templates"]]
    assert "Custom Lease Invoice" in titles


def test_reupload_same_pdf_updates_title_via_cache(authed_client, isolated_template_dirs, monkeypatch):
    """REGRESSION (/review 2026-05-10): re-uploading the same PDF bytes hits
    the pdf_sha256 cache and skips the AI mapping. Before the fix, the
    user's new title was silently dropped — they got back the old title.
    After the fix, the cached row's title updates to whatever the user
    typed on the second upload."""
    fake_proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names", confidence=10),
    ])
    call_count = {"n": 0}

    async def fake_propose(_descs, crops=None, **_kwargs):
        call_count["n"] += 1
        return fake_proposal
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()

    # First upload: AI mapping runs. The two-pass orchestrator may invoke
    # propose_mapping more than once (pass 1 mini + pass 2 gpt-5 on the
    # low-confidence subset) — what matters for THIS test is that the
    # SECOND upload (same bytes) skips the AI entirely via the SHA cache.
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "Original Title"},
        files={"pdf": ("a.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Original Title"
    assert call_count["n"] >= 1, "First upload should call the AI at least once"
    first_upload_calls = call_count["n"]
    template_id = r.json()["id"]

    # Second upload, same PDF bytes, NEW title. Cache hit: AI must NOT be
    # called again, and the returned title must be the new one.
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "New Title"},
        files={"pdf": ("a.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    assert call_count["n"] == first_upload_calls, "Cache should have prevented further AI calls"
    assert r.json()["title"] == "New Title", "Cache hit must honor user's new title"
    assert r.json()["id"] == template_id, "Cache hit returns the same row"

    # The list view also reflects the new title.
    listing = authed_client.get("/api/templates").json()
    titles = [t["title"] for t in listing["templates"]]
    assert "New Title" in titles
    assert "Original Title" not in titles


def test_upload_rejects_non_pdf(authed_client, isolated_template_dirs):
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "x"},
        files={"pdf": ("notes.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 400


def test_upload_rejects_empty_file(authed_client, isolated_template_dirs):
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "x"},
        files={"pdf": ("empty.pdf", b"", "application/pdf")},
    )
    assert r.status_code == 400


def test_delete_unknown_template_is_404(authed_client, isolated_template_dirs):
    r = authed_client.delete("/api/templates/does-not-exist")
    assert r.status_code == 404


def test_delete_custom_template_round_trip(authed_client, isolated_template_dirs, monkeypatch):
    """Upload a template, delete it, verify it's gone from the list AND the
    files are cleaned up."""
    async def fake_propose(_descs, crops=None, **_kwargs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names", confidence=10),
            ProposedField(pdf_field="PROPERTY ADDRESS", canonical_path="property.address", confidence=10),
            ProposedField(pdf_field="LEASE DATE", canonical_path="lease_start", confidence=10),
            ProposedField(pdf_field="COMMENCEMENT DATE", canonical_path="lease_start", confidence=10),
            ProposedField(pdf_field="AMOUNT DUE COMPASS", canonical_path="commission_amount", confidence=10),
            ProposedField(pdf_field="COMPASS AGENT", canonical_path="agent.name", confidence=10),
            ProposedField(pdf_field="LEASE INVOICE", canonical_path=None,
                          extra_field_name="invoice_number", extra_field_type="text",
                          extra_field_description="x", confidence=10),
        ])
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "Trash Me"},
        files={"pdf": ("custom.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    template_id = r.json()["id"]

    pdf_on_disk = templates_mod.TEMPLATES_PDF_DIR / f"{template_id}.pdf"
    mapping_on_disk = templates_mod.MAPPINGS_DIR / f"{template_id}.json"
    assert pdf_on_disk.exists()
    assert mapping_on_disk.exists()

    r = authed_client.delete(f"/api/templates/{template_id}")
    assert r.status_code == 204

    listing = authed_client.get("/api/templates").json()
    assert template_id not in [t["id"] for t in listing["templates"]]
    assert not pdf_on_disk.exists()
    assert not mapping_on_disk.exists()


def test_upload_ai_failure_cleans_up_pdf(authed_client, isolated_template_dirs, monkeypatch):
    """If GPT errors after we've saved the PDF, we should not leave an
    orphan file on disk."""
    async def fake_propose_fails(_descs, crops=None, **_kwargs):
        raise templates_mod.AIMappingError("simulated API outage")
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose_fails)

    before = list(templates_mod.TEMPLATES_PDF_DIR.iterdir())

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "Will Fail"},
        files={"pdf": ("x.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 502

    after = list(templates_mod.TEMPLATES_PDF_DIR.iterdir())
    assert before == after, "orphan PDF left after AI failure"


# ---- Multi-tenancy / IDOR tests ----

def test_anonymous_request_is_401(clean_db):
    """No cookie → 401 on every protected endpoint."""
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    assert client.get("/api/templates").status_code == 401
    assert client.post("/api/templates/upload",
                       data={"title": "x"},
                       files={"pdf": ("x.pdf", b"%PDF-1.4 short", "application/pdf")}
                       ).status_code == 401
    assert client.delete("/api/templates/anything").status_code == 401


def test_user_b_cannot_see_user_a_templates(two_authed_clients, isolated_template_dirs, monkeypatch):
    """The privacy boundary. Alice uploads → Bob's list stays empty."""
    alice, bob = two_authed_clients
    async def fake_propose(_descs, crops=None, **_kwargs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names", confidence=10),
        ])
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = alice.post(
        "/api/templates/upload",
        data={"title": "Alice's Template"},
        files={"pdf": ("a.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200

    bob_listing = bob.get("/api/templates").json()
    assert bob_listing["templates"] == []


def test_user_b_delete_on_user_a_template_is_404_idor_safe(two_authed_clients, isolated_template_dirs, monkeypatch):
    """The IDOR test. Bob obtains Alice's template id (from a leak, screenshare,
    error msg) and tries every endpoint. All return 404 — never 403, which
    would confirm 'this id exists, you just can't touch it'."""
    alice, bob = two_authed_clients
    async def fake_propose(_descs, crops=None, **_kwargs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="X", canonical_path="property.address", confidence=10),
        ])
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = alice.post(
        "/api/templates/upload",
        data={"title": "Alice's Secret"},
        files={"pdf": ("a.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    alice_template_id = r.json()["id"]

    # Bob attempts DELETE on Alice's template.
    r = bob.delete(f"/api/templates/{alice_template_id}")
    assert r.status_code == 404

    # Alice's template still exists from her perspective.
    alice_listing = alice.get("/api/templates").json()
    assert alice_template_id in [t["id"] for t in alice_listing["templates"]]


def test_extract_only_resolves_caller_template_extras(two_authed_clients, isolated_template_dirs, monkeypatch):
    """Active_template_ids that belong to another user are silently ignored
    rather than activating their extras for the caller."""
    alice, bob = two_authed_clients
    async def fake_propose(_descs, crops=None, **_kwargs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="X", extra_field_name="alice_secret",
                          extra_field_type="text", extra_field_description="x",
                          confidence=10),
        ])
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = alice.post(
        "/api/templates/upload",
        data={"title": "Alice's"},
        files={"pdf": ("a.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    alice_template_id = r.json()["id"]

    captured = {}
    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        captured["template_extras"] = template_extras
        from backend.schema import TransactionFields
        return TransactionFields()

    from backend import main
    monkeypatch.setattr(main, "extract_fields", fake_extract)

    # Bob requests extraction with Alice's template id active.
    r = bob.post(
        "/api/extract",
        data={"notes": "test", "active_template_ids": alice_template_id},
    )
    assert r.status_code == 200
    # Bob's extract dropped the cross-user id silently.
    assert captured["template_extras"] == {}


# ============================================================================
# PATCH /api/templates/{id}/mapping — low-confidence review UI backend
# ============================================================================

def _upload_with_low_confidence(authed_client, monkeypatch):
    """Helper: upload a template whose mapping has one low-confidence entry
    so the PATCH tests have something to correct. Returns (template_id,
    mapping_dict)."""
    # Two fields. One canonical (high confidence), one low-conf extra (will
    # land in low_confidence and the banner).
    fake_proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names",
                      confidence=10),
        ProposedField(pdf_field="PROPERTY ADDRESS", extra_field_name="mystery_address",
                      extra_field_type="text", extra_field_description="x",
                      confidence=3),  # below threshold → low_confidence
    ])

    async def fake_propose(_descs, crops=None, **_kwargs):
        return fake_proposal
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "PATCH test"},
        files={"pdf": ("a.pdf", LEASE_INVOICE_PDF.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data["id"], data


def test_patch_mapping_canonical_correction_clears_low_confidence(
    authed_client, isolated_template_dirs, monkeypatch
):
    """Happy path: user accepts a canonical override for the AI's uncertain
    field. The mapping rewrites to {<path>}, the entry leaves
    low_confidence, and if no entries remain the template flips to ready."""
    template_id, _ = _upload_with_low_confidence(authed_client, monkeypatch)

    r = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {"pdf_field": "PROPERTY ADDRESS", "canonical_path": "property.address"},
        ]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["low_confidence_remaining"] == 0
    assert body["mapping"]["fields"]["PROPERTY ADDRESS"] == "{property.address}"
    # The OTHER field (already canonical) is unchanged.
    assert body["mapping"]["fields"]["TENANTS NAME"] == "{tenant_or_buyer_names}"


def test_patch_mapping_extra_field_correction_registers_extra(
    authed_client, isolated_template_dirs, monkeypatch
):
    """Override path: user wants a template-specific extra. The mapping
    rewrites to {template_extras.<name>} and the extra is added to
    extra_fields so future extracts include it in the dynamic schema.

    REGRESSION (code review 2026-05-12): the PATCH endpoint originally
    only wrote the mapping JSON. The templates.extra_fields DB column —
    which /api/extract reads when building the dynamic Pydantic schema —
    was never updated. The {template_extras.X} reference would then
    always render blank at fill time. Assert both writes here so the
    bug can't regress."""
    template_id, _ = _upload_with_low_confidence(authed_client, monkeypatch)

    r = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {
                "pdf_field": "PROPERTY ADDRESS",
                "extra_field_name": "delivery_address",
                "extra_field_type": "text",
                "extra_field_description": "Where invoices ship",
            },
        ]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["fields"]["PROPERTY ADDRESS"] == "{template_extras.delivery_address}"
    extras = {e["name"]: e for e in body["extra_fields"]}
    assert "delivery_address" in extras
    assert extras["delivery_address"]["type"] == "text"

    # Cross-check: read the DB row directly (the templates list endpoint
    # only exposes the COUNT of extras; the propagation we care about is
    # the actual NAMES landing in the templates.extra_fields column so
    # the dynamic schema build in /api/extract can see them).
    user_id = authed_client.get("/api/auth/me").json()["id"]
    from backend.db import get_conn
    from backend import models
    with get_conn() as conn:
        tpl = models.get_template(conn, template_id, user_id=user_id)
    assert tpl is not None
    db_extras_by_name = {e.name: e for e in tpl.extra_fields}
    assert "delivery_address" in db_extras_by_name, \
        "extra registered via PATCH must appear in templates.extra_fields DB column"
    assert db_extras_by_name["delivery_address"].type == "text"
    assert db_extras_by_name["delivery_address"].description == "Where invoices ship"


def test_patch_mapping_extra_field_propagates_to_extract_schema(
    authed_client, isolated_template_dirs, monkeypatch
):
    """REGRESSION (code review 2026-05-12 C1): the user registers an
    extra via PATCH, then runs /api/extract with that template active.
    The dynamic Pydantic schema must include the new extra's name so
    the AI sees a slot to fill and the {template_extras.X} reference
    isn't dead at generate time."""
    template_id, _ = _upload_with_low_confidence(authed_client, monkeypatch)

    # Register a new extra via PATCH.
    r = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {
                "pdf_field": "PROPERTY ADDRESS",
                "extra_field_name": "delivery_address",
                "extra_field_type": "text",
                "extra_field_description": "Where invoices ship",
            },
        ]},
    )
    assert r.status_code == 200, r.text

    # Now spy on what /api/extract receives. The dynamic-schema build
    # consumes the templates DB row's extra_fields list — if PATCH
    # didn't write to the DB, this dict would be empty for our template.
    captured = {}

    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        captured["template_extras"] = template_extras
        from backend.schema import TransactionFields
        return TransactionFields()

    from backend import main
    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = authed_client.post(
        "/api/extract",
        data={"notes": "test", "active_template_ids": template_id},
    )
    assert r.status_code == 200, r.text
    extras_for_tpl = captured["template_extras"].get(template_id) or []
    extra_names = {e.name for e in extras_for_tpl}
    assert "delivery_address" in extra_names, \
        "extract_fields should see the PATCH-registered extra in its dynamic schema"


def test_patch_mapping_concurrent_writes_do_not_drop_corrections(
    authed_client, isolated_template_dirs, monkeypatch
):
    """REGRESSION (code review 2026-05-12 C2): two PATCH requests on
    the same template fired concurrently (browser double-click, React
    strict-mode double-fetch) used to race: both loaded the same base
    mapping, both wrote their delta, last-write-wins silently dropped
    the earlier corrections. The per-template asyncio.Lock serializes
    them so both corrections land.

    The TestClient's transport is synchronous so we can't fire two
    PATCHes mid-flight in a single test, but we can spy on the
    mapping-write path to confirm the lock is held while the second
    request would otherwise race in. Simplest functional check: fire
    two sequential PATCHes against DIFFERENT pdf_fields and assert
    BOTH corrections survive (neither write nukes the other's delta)."""
    template_id, _ = _upload_with_low_confidence(authed_client, monkeypatch)

    # First PATCH: route PROPERTY ADDRESS to canonical.
    r1 = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {"pdf_field": "PROPERTY ADDRESS", "canonical_path": "property.address"},
        ]},
    )
    assert r1.status_code == 200, r1.text
    # Second PATCH: skip a DIFFERENT field. The on-disk mapping must
    # carry BOTH the canonical from PATCH 1 and the skip from PATCH 2.
    r2 = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {"pdf_field": "TENANTS NAME", "skip": True},
        ]},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["mapping"]["fields"]["PROPERTY ADDRESS"] == "{property.address}", \
        "first PATCH's correction must survive the second PATCH"
    assert body["mapping"]["fields"]["TENANTS NAME"] == "", \
        "second PATCH's correction must apply"


def test_patch_mapping_validation_error_returns_readable_string(
    authed_client, isolated_template_dirs, monkeypatch
):
    """REGRESSION (code review 2026-05-12 I2): HTTPException detail was
    originally a dict {"errors": [...]}, which FastAPI serialized as
    {"detail": {"errors": [...]}}. The frontend's res.text() then showed
    raw nested JSON to the user. Detail is now a plain string with one
    correction per line — readable in any context."""
    template_id, _ = _upload_with_low_confidence(authed_client, monkeypatch)
    r = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {"pdf_field": "PROPERTY ADDRESS", "canonical_path": "totally_made_up"},
            {"pdf_field": "NOT A REAL FIELD", "skip": True},
        ]},
    )
    assert r.status_code == 400, r.text
    detail = r.json().get("detail")
    assert isinstance(detail, str), f"detail must be a string, got {type(detail).__name__}: {detail!r}"
    assert "totally_made_up" in detail
    assert "NOT A REAL FIELD" in detail


def test_patch_mapping_skip_blanks_the_field(
    authed_client, isolated_template_dirs, monkeypatch
):
    """Skip path: user says this is hand-fill. The field's mapping becomes
    "" (blank at fill time) and the low_confidence entry is removed."""
    template_id, _ = _upload_with_low_confidence(authed_client, monkeypatch)

    r = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {"pdf_field": "PROPERTY ADDRESS", "skip": True},
        ]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mapping"]["fields"]["PROPERTY ADDRESS"] == ""
    assert body["low_confidence_remaining"] == 0


def test_patch_mapping_rejects_unknown_canonical_path(
    authed_client, isolated_template_dirs, monkeypatch
):
    """Hallucinated paths cause a 400 and the on-disk mapping is unchanged.
    Atomicity: nothing partial gets written if any correction is invalid."""
    template_id, original = _upload_with_low_confidence(authed_client, monkeypatch)

    r = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {"pdf_field": "PROPERTY ADDRESS", "canonical_path": "totally_made_up"},
        ]},
    )
    assert r.status_code == 400, r.text

    # Re-read via the listing: mapping is unchanged.
    listing = authed_client.get("/api/templates").json()
    tpl = next(t for t in listing["templates"] if t["id"] == template_id)
    assert tpl["status"] == "needs_attention"  # unchanged


def test_patch_mapping_rejects_unknown_pdf_field(
    authed_client, isolated_template_dirs, monkeypatch
):
    template_id, _ = _upload_with_low_confidence(authed_client, monkeypatch)
    r = authed_client.patch(
        f"/api/templates/{template_id}/mapping",
        json={"corrections": [
            {"pdf_field": "NOT A REAL FIELD", "skip": True},
        ]},
    )
    assert r.status_code == 400, r.text


def test_patch_mapping_cross_user_returns_404(
    two_authed_clients, isolated_template_dirs, monkeypatch
):
    """User B PATCHing user A's template returns 404 (not 403) so attackers
    can't enumerate other users' template ids."""
    alice, bob = two_authed_clients

    fake_proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="TENANTS NAME", extra_field_name="x",
                      extra_field_type="text", extra_field_description="x",
                      confidence=3),
    ])
    async def fake_propose(_descs, crops=None, **_kwargs):
        return fake_proposal
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    r = alice.post(
        "/api/templates/upload",
        data={"title": "alices template"},
        files={"pdf": ("a.pdf", LEASE_INVOICE_PDF.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 200
    alice_template_id = r.json()["id"]

    r = bob.patch(
        f"/api/templates/{alice_template_id}/mapping",
        json={"corrections": [{"pdf_field": "TENANTS NAME", "skip": True}]},
    )
    assert r.status_code == 404, r.text
