"""
Tests for the template upload pipeline (Pillar 2 chunk 4).

Three layers:
  - validate_pdf rejects bad inputs (encrypted, no AcroForm, malformed)
  - proposal_to_mapping_file translates AI output into the on-disk shape
    correctly (canonical paths vs extras vs the unmapped fallback)
  - the FastAPI endpoints work end-to-end with the OpenAI call mocked

The OpenAI call is the slow + expensive part, so we monkeypatch
templates.propose_mapping to return a hand-built ProposedMapping instead of
hitting the real API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from backend import db, models, templates as templates_mod
from backend.main import app
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
    reader = validate_pdf(pdf_bytes)
    # walk_fields was already called inside; sanity check that it found fields
    from backend.pdf_introspect import walk_fields
    assert len(walk_fields(reader)) > 0


def test_validate_pdf_rejects_garbage():
    with pytest.raises(TemplateUploadError, match="could not parse"):
        validate_pdf(b"this is not a pdf at all")


def test_validate_pdf_rejects_pdf_without_acroform(tmp_path: Path):
    """Build a one-page PDF with NO form fields and confirm we reject it."""
    from pypdf import PdfReader, PdfWriter
    # Use a simple stream — pypdf can produce a blank PDF but we don't even
    # need that level of cleanliness. Just take any AcroForm PDF and strip
    # /AcroForm from the catalog.
    src = PdfReader(str(LEASE_INVOICE_PDF))
    writer = PdfWriter(clone_from=src)
    cat = writer._root_object
    if "/AcroForm" in cat:
        del cat["/AcroForm"]
    out = tmp_path / "no-form.pdf"
    with out.open("wb") as f:
        writer.write(f)
    with pytest.raises(TemplateUploadError, match="no fillable form fields"):
        validate_pdf(out.read_bytes())


# ---- Proposal translation ----

def _proposal_with_two_canonicals_and_one_extra() -> ProposedMapping:
    return ProposedMapping(fields=[
        ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names"),
        ProposedField(pdf_field="PROPERTY ADDRESS", canonical_path="property.address"),
        ProposedField(
            pdf_field="PET DEPOSIT",
            extra_field_name="pet_deposit",
            extra_field_type="money",
            extra_field_description="The pet deposit amount as written by the agent.",
        ),
    ])


def test_proposal_translates_canonical_paths_to_template_strings():
    proposal = _proposal_with_two_canonicals_and_one_extra()
    mapping, extras, unknown_paths = proposal_to_mapping_file(
        proposal, title="Test", source_pdf_filename="abc.pdf",
        filled_filename="test_filled.pdf",
    )
    # Canonical paths become {<path>} interpolation strings
    assert mapping.fields["TENANTS NAME"] == "{tenant_or_buyer_names}"
    assert mapping.fields["PROPERTY ADDRESS"] == "{property.address}"
    # Extras get nested under template_extras.<name>
    assert mapping.fields["PET DEPOSIT"] == "{template_extras.pet_deposit}"
    # And come back in the extras list with metadata preserved
    assert len(extras) == 1
    assert extras[0].name == "pet_deposit"
    assert extras[0].type == "money"
    # All canonical paths in the fixture are valid TransactionFields keys
    assert unknown_paths == []


def test_proposal_unmapped_field_renders_empty_string():
    """If the AI returns neither canonical_path nor extra_field_name (it
    shouldn't but we shouldn't crash), the field is left as ''."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="MYSTERY"),  # no canonical, no extra
    ])
    mapping, _extras, _unknown = proposal_to_mapping_file(
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
        ),
    ])
    _, extras, _unknown = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert extras[0].type == "text"


def test_proposal_unknown_canonical_path_is_demoted_to_unmapped():
    """If GPT hallucinates a canonical_path that isn't in TransactionFields/
    AgentProfile/computed values, we leave the field unmapped (empty string)
    AND surface it in unknown_paths so the upload pipeline can flag the
    template for human review instead of letting the user discover blank
    fields at fill time."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="GOOD", canonical_path="property.address"),
        ProposedField(pdf_field="BAD", canonical_path="borrower.full_name"),
        ProposedField(pdf_field="ALSO_BAD", canonical_path="totally_made_up"),
    ])
    mapping, extras, unknown_paths = proposal_to_mapping_file(
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
    legitimate canonical_paths even though they aren't TransactionFields keys.
    The allowlist must include them or the AI mapper would have its valid
    'today → today' proposals wrongly demoted."""
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="DATE_OF_SIGNING", canonical_path="today"),
        ProposedField(pdf_field="ADDR_LINE", canonical_path="property.address_full"),
        ProposedField(pdf_field="COUNTY_TAIL", canonical_path="county_suffix"),
        ProposedField(pdf_field="TENANT_1", canonical_path="tenant_1_name"),
    ])
    mapping, _extras, unknown_paths = proposal_to_mapping_file(
        proposal, title="x", source_pdf_filename="x.pdf", filled_filename="x.pdf",
    )
    assert mapping.fields["DATE_OF_SIGNING"] == "{today}"
    assert mapping.fields["COUNTY_TAIL"] == "{county_suffix}"
    assert unknown_paths == []


# ---- API endpoint tests (DB + endpoint, AI mocked) ----

@pytest.fixture
def app_with_temp_db(tmp_path, monkeypatch):
    """Point db.DB_PATH at a temp sqlite + run migrations. Also redirect the
    template files dirs so we don't pollute the real templates/ tree."""
    test_db = tmp_path / "test.sqlite"
    test_pdf_dir = tmp_path / "pdf"
    test_mapping_dir = tmp_path / "mappings"
    test_pdf_dir.mkdir()
    test_mapping_dir.mkdir()
    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(templates_mod, "TEMPLATES_PDF_DIR", test_pdf_dir)
    monkeypatch.setattr(templates_mod, "MAPPINGS_DIR", test_mapping_dir)
    db.run_migrations(test_db)
    yield TestClient(app)


def test_list_templates_includes_seeded_defaults(app_with_temp_db):
    r = app_with_temp_db.get("/api/templates")
    assert r.status_code == 200
    data = r.json()
    ids = {t["id"] for t in data["templates"]}
    assert {"lease_invoice", "lease_abstract", "tenant_rep", "multiboard"} <= ids
    for t in data["templates"]:
        if t["id"] in {"lease_invoice", "lease_abstract", "tenant_rep", "multiboard"}:
            assert t["is_default"] is True


def test_upload_template_happy_path(app_with_temp_db, monkeypatch):
    """End-to-end: real PDF + mocked AI call. Verifies the row is inserted,
    the mapping JSON is written, and the response shape is right."""
    fake_proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names"),
        ProposedField(pdf_field="PROPERTY ADDRESS", canonical_path="property.address"),
        ProposedField(pdf_field="LEASE DATE", canonical_path="lease_start"),
        ProposedField(pdf_field="COMMENCEMENT DATE", canonical_path="lease_start"),
        ProposedField(pdf_field="AMOUNT DUE COMPASS", canonical_path="commission_amount"),
        ProposedField(pdf_field="COMPASS AGENT", canonical_path="agent.name"),
        ProposedField(pdf_field="LEASE INVOICE", extra_field_name="invoice_number",
                      extra_field_type="text", extra_field_description="Invoice number"),
    ])

    async def fake_propose(_descs):
        return fake_proposal
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = app_with_temp_db.post(
        "/api/templates/upload",
        data={"title": "Custom Lease Invoice"},
        files={"pdf": ("custom.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Custom Lease Invoice"
    assert data["status"] == "pending_review"
    assert data["field_count"] == 7
    assert len(data["extra_fields"]) == 1
    assert data["extra_fields"][0]["name"] == "invoice_number"
    # Mapping shape sanity check
    assert data["mapping"]["fields"]["TENANTS NAME"] == "{tenant_or_buyer_names}"
    assert data["mapping"]["fields"]["LEASE INVOICE"] == "{template_extras.invoice_number}"

    # And it shows up in the list
    listing = app_with_temp_db.get("/api/templates").json()
    titles = [t["title"] for t in listing["templates"]]
    assert "Custom Lease Invoice" in titles


def test_upload_rejects_non_pdf(app_with_temp_db):
    r = app_with_temp_db.post(
        "/api/templates/upload",
        data={"title": "x"},
        files={"pdf": ("notes.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 400


def test_upload_rejects_empty_file(app_with_temp_db):
    r = app_with_temp_db.post(
        "/api/templates/upload",
        data={"title": "x"},
        files={"pdf": ("empty.pdf", b"", "application/pdf")},
    )
    assert r.status_code == 400


def test_delete_default_template_is_forbidden(app_with_temp_db):
    r = app_with_temp_db.delete("/api/templates/lease_invoice")
    assert r.status_code == 403
    # And it's still there
    listing = app_with_temp_db.get("/api/templates").json()
    ids = [t["id"] for t in listing["templates"]]
    assert "lease_invoice" in ids


def test_delete_unknown_template_is_404(app_with_temp_db):
    r = app_with_temp_db.delete("/api/templates/does-not-exist")
    assert r.status_code == 404


def test_delete_custom_template_round_trip(app_with_temp_db, monkeypatch):
    """Upload a template, delete it, verify it's gone from the list AND the
    files are cleaned up."""
    async def fake_propose(_descs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names"),
            ProposedField(pdf_field="PROPERTY ADDRESS", canonical_path="property.address"),
            ProposedField(pdf_field="LEASE DATE", canonical_path="lease_start"),
            ProposedField(pdf_field="COMMENCEMENT DATE", canonical_path="lease_start"),
            ProposedField(pdf_field="AMOUNT DUE COMPASS", canonical_path="commission_amount"),
            ProposedField(pdf_field="COMPASS AGENT", canonical_path="agent.name"),
            ProposedField(pdf_field="LEASE INVOICE", canonical_path=None,
                          extra_field_name="invoice_number", extra_field_type="text",
                          extra_field_description="x"),
        ])
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose)

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = app_with_temp_db.post(
        "/api/templates/upload",
        data={"title": "Trash Me"},
        files={"pdf": ("custom.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    template_id = r.json()["id"]

    # Confirm files exist
    pdf_on_disk = templates_mod.TEMPLATES_PDF_DIR / f"{template_id}.pdf"
    mapping_on_disk = templates_mod.MAPPINGS_DIR / f"{template_id}.json"
    assert pdf_on_disk.exists()
    assert mapping_on_disk.exists()

    # Delete
    r = app_with_temp_db.delete(f"/api/templates/{template_id}")
    assert r.status_code == 204

    # Row is gone, files are cleaned up
    listing = app_with_temp_db.get("/api/templates").json()
    assert template_id not in [t["id"] for t in listing["templates"]]
    assert not pdf_on_disk.exists()
    assert not mapping_on_disk.exists()


def test_upload_ai_failure_cleans_up_pdf(app_with_temp_db, monkeypatch):
    """If GPT errors after we've saved the PDF, we should not leave an
    orphan file on disk."""
    async def fake_propose_fails(_descs):
        raise templates_mod.AIMappingError("simulated API outage")
    monkeypatch.setattr(templates_mod, "propose_mapping", fake_propose_fails)

    before = list(templates_mod.TEMPLATES_PDF_DIR.iterdir())

    pdf_bytes = LEASE_INVOICE_PDF.read_bytes()
    r = app_with_temp_db.post(
        "/api/templates/upload",
        data={"title": "Will Fail"},
        files={"pdf": ("x.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 502

    after = list(templates_mod.TEMPLATES_PDF_DIR.iterdir())
    assert before == after, "orphan PDF left after AI failure"
