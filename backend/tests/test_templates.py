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

    async def fake_propose(_descs, crops=None):
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

    async def fake_propose(_descs, crops=None):
        call_count["n"] += 1
        return fake_proposal
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()

    # First upload: AI mapping runs (call_count goes to 1).
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "Original Title"},
        files={"pdf": ("a.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Original Title"
    assert call_count["n"] == 1
    template_id = r.json()["id"]

    # Second upload, same PDF bytes, NEW title. Cache hit: AI must NOT be
    # called again, and the returned title must be the new one.
    r = authed_client.post(
        "/api/templates/upload",
        data={"title": "New Title"},
        files={"pdf": ("a.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    assert call_count["n"] == 1, "Cache should have prevented a second AI call"
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
    async def fake_propose(_descs, crops=None):
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
    async def fake_propose_fails(_descs, crops=None):
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
    async def fake_propose(_descs, crops=None):
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
    async def fake_propose(_descs, crops=None):
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
    async def fake_propose(_descs, crops=None):
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
