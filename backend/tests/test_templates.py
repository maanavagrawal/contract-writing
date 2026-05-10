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
    reader = validate_pdf(pdf_bytes)
    from backend.pdf_introspect import walk_fields
    assert len(walk_fields(reader)) > 0


def test_validate_pdf_rejects_garbage():
    with pytest.raises(TemplateUploadError, match="could not parse"):
        validate_pdf(b"this is not a pdf at all")


def test_validate_pdf_rejects_pdf_without_acroform(tmp_path: Path):
    """Build a one-page PDF with NO form fields and confirm we reject it."""
    from pypdf import PdfReader, PdfWriter
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
    assert mapping.fields["TENANTS NAME"] == "{tenant_or_buyer_names}"
    assert mapping.fields["PROPERTY ADDRESS"] == "{property.address}"
    assert mapping.fields["PET DEPOSIT"] == "{template_extras.pet_deposit}"
    assert len(extras) == 1
    assert extras[0].name == "pet_deposit"
    assert extras[0].type == "money"
    assert unknown_paths == []


def test_proposal_unmapped_field_renders_empty_string():
    proposal = ProposedMapping(fields=[
        ProposedField(pdf_field="MYSTERY"),
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
    """If GPT hallucinates a canonical_path, we leave the field unmapped AND
    surface it in unknown_paths so the upload pipeline can flag the template
    for human review instead of letting the user discover blank fields at
    fill time."""
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
    legitimate canonical_paths even though they aren't TransactionFields keys."""
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
    r = authed_client.post(
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
    assert data["mapping"]["fields"]["TENANTS NAME"] == "{tenant_or_buyer_names}"
    assert data["mapping"]["fields"]["LEASE INVOICE"] == "{template_extras.invoice_number}"

    listing = authed_client.get("/api/templates").json()
    titles = [t["title"] for t in listing["templates"]]
    assert "Custom Lease Invoice" in titles


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
    async def fake_propose_fails(_descs):
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
    async def fake_propose(_descs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="TENANTS NAME", canonical_path="tenant_or_buyer_names"),
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
    async def fake_propose(_descs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="X", canonical_path="property.address"),
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
    async def fake_propose(_descs):
        return ProposedMapping(fields=[
            ProposedField(pdf_field="X", extra_field_name="alice_secret",
                          extra_field_type="text", extra_field_description="x"),
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
    async def fake_extract(notes, images=None, template_extras=None):
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
