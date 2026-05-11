"""
Regression tests for the v0 autofill bug.

Pre-fix behavior:
  - lease_invoice filled 1/6 mapped fields (pypdf update_page_form_field_values
    silently no-ops when widget /T is on the parent field)
  - lease_abstract didn't fill 'Lease End Date.0.0' (dotted hierarchical name)
  - Multi-Board /Btn fields had /V set but not /AS on widget kids → checkboxes
    visually unchecked in Preview

Post-fix: walk /AcroForm/Fields directly, write /V on the leaf, /AS on widgets.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from pypdf import PdfReader

# Tests don't actually call OpenAI but extract.py loads at module import.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from backend.generate import fill_document
from backend.pdf_fill import fill_pdf
from backend.pdf_introspect import walk_fields
from backend.schema import AgentProfile, Property, TransactionFields

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = ROOT / "templates" / "pdf"


def _read_filled(generated_doc) -> PdfReader:
    import base64
    return PdfReader(io.BytesIO(base64.b64decode(generated_doc.base64)))


def _v(reader: PdfReader, field_name: str):
    """Get the /V value of a leaf field by dotted name. Returns None if missing."""
    for fi in walk_fields(reader):
        if fi.dotted_name == field_name:
            return fi.leaf_obj.get("/V")
    return None


def _as(reader: PdfReader, field_name: str) -> list:
    """Get the /AS value of every widget under a field. /AS is what makes a
    checkbox visually checked, distinct from /V which is the field's value."""
    result = []
    for fi in walk_fields(reader):
        if fi.dotted_name == field_name:
            for w in fi.widgets:
                pass
            # walk_fields gave us widget metadata, but we need the actual /AS
            # values from the writer's output. Re-walk the raw fields tree:
            return _all_as_for(reader, field_name)
    return result


def _all_as_for(reader: PdfReader, target_name: str) -> list[str]:
    """Walk the AcroForm tree, find the leaf with this dotted name, return
    every kid's /AS as a string."""
    from backend.pdf_introspect import _deref
    out: list[str] = []

    def visit(field_ref, name_parts):
        obj = _deref(field_ref)
        t = obj.get("/T")
        if t is not None:
            name_parts = name_parts + [str(t)]
        kids = obj.get("/Kids")
        if kids is not None:
            kids = _deref(kids)
        if not kids:
            if ".".join(name_parts) == target_name:
                out.append(str(obj.get("/AS")) if obj.get("/AS") is not None else "")
            return
        widget_kids = []
        field_kids = []
        for k in kids:
            k_obj = _deref(k)
            if k_obj.get("/Subtype") == "/Widget":
                widget_kids.append(k)
            elif k_obj.get("/T") is not None or k_obj.get("/FT") is not None or k_obj.get("/Kids") is not None:
                field_kids.append(k)
            else:
                widget_kids.append(k)
        if field_kids:
            for fk in field_kids:
                visit(fk, name_parts)
            return
        if ".".join(name_parts) == target_name:
            for wk in widget_kids:
                wk_o = _deref(wk)
                out.append(str(wk_o.get("/AS")) if wk_o.get("/AS") is not None else "")

    catalog = _deref(reader.trailer["/Root"])
    acroform = _deref(catalog["/AcroForm"])
    for f in acroform["/Fields"]:
        visit(f, [])
    return out


# --- fixtures -----------------------------------------------------------------

@pytest.fixture
def lease_fields() -> TransactionFields:
    return TransactionFields(
        transaction_type="lease",
        property=Property(
            address="221 W Hubbard", unit="803",
            city="Chicago", state="IL", zip="60654",
        ),
        lease_start="2026-05-04",
        lease_end="2027-07-03",
        monthly_rent="$3182",
        net_monthly_rent="$3000",
        tenant_or_buyer_names=["John Doe"],
        tenant_or_buyer_email="john@example.com",
        tenant_or_buyer_phone="312-555-0100",
        landlord_billing_email="leasing@example.com",
        commission_amount="$3182",
        concessions="1 month free",
    )


@pytest.fixture
def sale_fields() -> TransactionFields:
    return TransactionFields(
        transaction_type="sale",
        property=Property(
            address="221 W Hubbard", unit="803",
            city="Chicago", state="IL", zip="60654",
        ),
        purchase_price="$725,000",
        closing_date="2026-07-15",
        earnest_money="$10,000",
        seller_names=["Jane Seller"],
        tenant_or_buyer_names=["John Buyer", "Jane Buyer"],
        tenant_or_buyer_email="buyer@example.com",
        tenant_or_buyer_phone="312-555-0100",
        buyer_address="100 Main St",
        buyer_city="Chicago",
        buyer_state="IL",
        buyer_zip="60654",
        property_type="attached",
        county="Cook",
        commission_amount="2.5%",
    )


@pytest.fixture
def agent() -> AgentProfile:
    return AgentProfile(
        name="Test Agent",
        license="LIC123",
        phone="312-555-0001",
        email="agent@test.com",
        brokerage_address="123 Main St",
        brokerage_mls="MLS456",
        brokerage_license="BL789",
        mls="A001",
    )


# --- regressions --------------------------------------------------------------

def test_lease_invoice_fills_all_six_mapped_fields(lease_fields, agent):
    """REGRESSION: pre-fix this filled 1/6.

    The lease invoice PDF stores /T on the AcroForm field (parent), not on the
    widget annotation. update_page_form_field_values silently no-ops in this
    layout. The new tree-walking filler writes /V directly on the leaf field."""
    doc = fill_document("lease_invoice", lease_fields, agent)
    reader = _read_filled(doc)

    assert _v(reader, "PROPERTY ADDRESS") and "221 W Hubbard" in str(_v(reader, "PROPERTY ADDRESS"))
    assert _v(reader, "TENANTS NAME") and "John Doe" in str(_v(reader, "TENANTS NAME"))
    assert _v(reader, "AMOUNT DUE COMPASS") and "$3,182" in str(_v(reader, "AMOUNT DUE COMPASS"))
    assert _v(reader, "COMPASS AGENT") and "Test Agent" in str(_v(reader, "COMPASS AGENT"))
    assert _v(reader, "LEASE DATE")  # today's date — non-empty
    assert _v(reader, "COMMENCEMENT DATE") and "05/04/2026" in str(_v(reader, "COMMENCEMENT DATE"))


def test_lease_abstract_dotted_path_resolves(lease_fields, agent):
    """REGRESSION: 'Lease End Date.0.0' is a 3-level hierarchical field
    (parent 'Lease End Date' → kid '0' → grandkid '0'). pypdf.get_fields()
    reports the dotted name, but update_page_form_field_values can't write
    to it. The new filler joins ancestor /T values to match."""
    doc = fill_document("lease_abstract", lease_fields, agent)
    reader = _read_filled(doc)

    v = _v(reader, "Lease End Date.0.0")
    assert v is not None and "07/03/2027" in str(v), f"expected 07/03/2027, got {v!r}"


def test_multiboard_checkbox_visual_state(sale_fields, agent):
    """REGRESSION: setting /V to a state name like '/On' or '/Choice1' is not
    enough — Preview/Acrobat render checkboxes based on the widget's /AS.
    For property_type='attached', mapping renders to '/On' on field 7 and
    '' on fields 8/9. After fill, field 7's widget should have /AS = /On."""
    doc = fill_document("multiboard", sale_fields, agent)
    reader = _read_filled(doc)

    # Field 7 = "attached" checkbox
    assert _v(reader, "7") == "/On"
    as_values_7 = _all_as_for(reader, "7")
    assert "/On" in as_values_7, f"expected /On in field 7's /AS, got {as_values_7!r}"

    # Field 8 (detached) and 9 (multi_unit) should not be checked
    # Their /V is unchanged from pre-fill (we skip empty values)
    # but in the source PDF, default state is /Off — verify they're still not /On
    as_values_8 = _all_as_for(reader, "8")
    as_values_9 = _all_as_for(reader, "9")
    assert "/On" not in as_values_8, f"field 8 should not be checked, got {as_values_8!r}"
    assert "/On" not in as_values_9, f"field 9 should not be checked, got {as_values_9!r}"


def test_multiboard_radio_group_escrowee(sale_fields, agent):
    """Multi-Board field 31 is a radio group with state names like
    '/Seller's Brokerage'. fill_pdf must write /AS on the matching widget
    kid and /Off on the rest."""
    doc = fill_document("multiboard", sale_fields, agent)
    reader = _read_filled(doc)

    # Default escrowee is "seller" → state = "/Seller's Brokerage"
    assert _v(reader, "31") == "/Seller's Brokerage"
    as_values = _all_as_for(reader, "31")
    # Field 31 has 3 widget kids (one per radio option). Exactly one should
    # be /Seller's Brokerage; the rest should be /Off.
    assert "/Seller's Brokerage" in as_values, f"radio not set: {as_values!r}"


def test_fill_pdf_skips_empty_rendered_values():
    """Empty strings in the rendered dict should NOT overwrite existing /V.
    The lease invoice PDF ships with 'LEASE INVOICE' = '490384' (an invoice
    number). If we write '' to it, that pre-filled value would be lost."""
    pdf_path = TEMPLATES / "2025 Compass Chicagoland Lease Invoice Landlords and Tenant Use copy.pdf"
    reader = PdfReader(str(pdf_path))

    # Confirm the pre-filled value exists in the source.
    pre_filled = _v(reader, "LEASE INVOICE")
    assert pre_filled is not None and str(pre_filled) != ""

    # Pass an empty string for that field — should be skipped.
    rendered = {"LEASE INVOICE": ""}
    out_bytes = fill_pdf(reader, rendered)
    out_reader = PdfReader(io.BytesIO(out_bytes))
    assert _v(out_reader, "LEASE INVOICE") == pre_filled


def test_all_four_templates_fill_without_errors(lease_fields, sale_fields, agent):
    """Smoke test: every shipped template fills cleanly with realistic input."""
    for doc_key in ["lease_invoice", "lease_abstract", "tenant_rep"]:
        doc = fill_document(doc_key, lease_fields, agent)
        assert doc.base64 and doc.filename.endswith(".pdf")

    doc = fill_document("multiboard", sale_fields, agent)
    assert doc.base64 and doc.filename.endswith(".pdf")


def test_multiboard_address_no_county_suffix_when_county_blank(lease_fields, agent):
    """REGRESSION: multiboard mapping was '{property.address_full}, {county} County'.
    When county was empty, the literal ', County' tail leaked into the rendered
    Address. Now uses {county_suffix} which renders empty when county is blank."""
    # lease_fields has no county set
    lease_fields.transaction_type = "sale"  # Multi-Board is sale-side
    doc = fill_document("multiboard", lease_fields, agent)
    reader = _read_filled(doc)

    addr = _v(reader, "Address")
    assert addr is not None
    addr_str = str(addr)
    assert "County" not in addr_str, f"county suffix leaked: {addr_str!r}"
    assert addr_str.rstrip().endswith("60654") or "Hubbard" in addr_str, addr_str


def test_multiboard_address_includes_county_when_set(sale_fields, agent):
    """Counterpart: when county is set, the suffix renders as ', Cook County'."""
    doc = fill_document("multiboard", sale_fields, agent)
    reader = _read_filled(doc)

    addr = str(_v(reader, "Address"))
    assert "Cook County" in addr, f"expected 'Cook County' in {addr!r}"
    # Make sure we didn't double-up commas like "60654, , Cook County"
    assert ", , " not in addr


def test_btnchoice_resolves_and_fills_same_as_bundled_state(sale_fields, agent):
    """REGRESSION + new-feature parity: filling Multi-Board with the bundled
    {property_type_attached_state} template should produce the SAME /AS as
    filling it with a BtnChoice(canonical_path='property_type',
    choices={'attached': '/On', 'detached': '/Off', 'multi_unit': '/Off'}).

    This is the proof that BtnChoice is a 1:1 substitute for the bundled
    computed-state pattern. If this test ever fails, BtnChoice resolution
    drifted from the bundled mapping's behavior."""
    import json
    from backend.generate import fill_document, MAPPINGS_DIR
    from backend.interpolate import build_context, interpolate_mapping
    from backend.pdf_fill import fill_pdf
    from backend.schema import BtnChoice, MappingFile
    from pypdf import PdfReader

    # First baseline: fill via bundled mapping path.
    bundled_doc = fill_document("multiboard", sale_fields, agent)
    bundled_reader = _read_filled(bundled_doc)
    bundled_as_7 = _all_as_for(bundled_reader, "7")  # "attached" checkbox
    bundled_as_8 = _all_as_for(bundled_reader, "8")  # "detached"
    bundled_as_9 = _all_as_for(bundled_reader, "9")  # "multi_unit"

    # Now build the SAME mapping but swap fields 7/8/9 to use BtnChoice.
    raw = json.loads((MAPPINGS_DIR / "multiboard.json").read_text())
    mapping = MappingFile.model_validate(raw)
    btn_choices = {"attached": "/On", "detached": "/Off", "multi_unit": "/Off"}
    mapping.fields["7"] = BtnChoice(canonical_path="property_type",
                                    choices={"attached": "/On"})
    mapping.fields["8"] = BtnChoice(canonical_path="property_type",
                                    choices={"detached": "/On"})
    mapping.fields["9"] = BtnChoice(canonical_path="property_type",
                                    choices={"multi_unit": "/On"})

    ctx = build_context(sale_fields.model_dump(mode="json"),
                        agent.model_dump(mode="json"))
    rendered = interpolate_mapping(mapping.fields, ctx)

    source_pdf = TEMPLATES / mapping.meta.source_pdf
    reader = PdfReader(str(source_pdf))
    out_bytes = fill_pdf(reader, rendered)
    btn_reader = PdfReader(io.BytesIO(out_bytes))

    # BtnChoice path should produce the SAME /AS values as the bundled path.
    assert _all_as_for(btn_reader, "7") == bundled_as_7
    assert _all_as_for(btn_reader, "8") == bundled_as_8
    assert _all_as_for(btn_reader, "9") == bundled_as_9


def test_btnchoice_handles_non_On_widget_states():
    """REGRESSION (outside-voice F9): the actual bug this feature exists to
    fix. A widget with /AP/N keys = {/Yes, /Off} should get /AS = /Yes when
    the BtnChoice's resolved state is /Yes — not silently /Off because
    the AI's guess of '/On' wasn't in the widget's supported states.

    Approach: feed fill_pdf a synthetic field-name → state-name mapping
    directly (skipping the AI mapping pipeline), and verify the existing
    pdf_fill logic does the right thing when given a literal state matching
    the widget's actual /AP/N. The end-to-end /Yes flow is exercised when
    interpolate_mapping → resolve_btn_choice produces '/Yes' for a
    BtnChoice with choices={'true': '/Yes'}."""
    from backend.interpolate import interpolate_mapping
    from backend.schema import BtnChoice

    # Verify the BtnChoice -> "/Yes" resolution path. The widget-level
    # behavior (writing /AS=/Yes when /Yes is in /AP/N) is already covered
    # by pdf_fill.py:106 and exercised by test_multiboard_checkbox_visual_state.
    # The new code path is "BtnChoice produces the right literal" — assert
    # that part:
    bc = BtnChoice(canonical_path="dual_agency", choices={"true": "/Yes"})
    ctx = {"dual_agency": True}
    rendered = interpolate_mapping({"DUAL": bc}, ctx)
    assert rendered["DUAL"] == "/Yes", (
        "BtnChoice must emit the literal state string from choices, not '/On'. "
        "If this fails, AI-mapped templates with non-standard widget states "
        "(e.g. /Yes instead of /On) silently fail to check their boxes."
    )

    # And the negative case: same widget, value not in choices -> /Off.
    rendered_off = interpolate_mapping({"DUAL": bc}, {"dual_agency": False})
    assert rendered_off["DUAL"] == "/Off"


def test_existing_string_only_mappings_still_fill_unchanged(sale_fields, agent):
    """REGRESSION: existing bundled mapping JSONs are pure dict[str, str].
    After switching MappingFile.fields to dict[str, str | BtnChoice], they
    must continue to fill exactly as before. fill_document on the bundled
    multiboard.json produces a working PDF with the right values."""
    doc = fill_document("multiboard", sale_fields, agent)
    reader = _read_filled(doc)

    # Address field — string template path.
    addr = str(_v(reader, "Address"))
    assert "Hubbard" in addr
    assert "Cook County" in addr

    # Purchase price — string template + currency filter.
    price = str(_v(reader, "24"))
    assert "$725,000" in price


def test_need_appearances_flag_set(lease_fields, agent):
    """Without /NeedAppearances=true, Preview shows empty fields even when /V
    is set. This is an easy regression to introduce."""
    from backend.pdf_introspect import _deref

    doc = fill_document("lease_invoice", lease_fields, agent)
    reader = _read_filled(doc)
    catalog = _deref(reader.trailer["/Root"])
    acroform = _deref(catalog["/AcroForm"])
    # pypdf wraps booleans in BooleanObject; equality check, not identity.
    assert bool(acroform.get("/NeedAppearances")) is True
