"""
Tests for generate.py + /api/generate.

Two layers:
  - Pure mapping-load tests (no DB, no auth)
  - /api/generate end-to-end with auth cookie + a real template row that
    points at one of the shipped mapping JSONs in backend/mappings/.

The shipped mapping JSONs (lease_invoice, lease_abstract, tenant_rep,
multiboard) still live on disk for users who upload those forms — they're
not seeded into anyone's account, but they remain valid mappings if a row
is inserted manually pointing at them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import models
from backend.generate import InvalidMapping, UnknownDocument, _load_mapping
from backend.schema import MappingFile


# ---- Pure mapping-load tests (no DB, no auth) ----

def test_load_mapping_returns_pydantic_model():
    m = _load_mapping("lease_invoice")
    assert isinstance(m, MappingFile)
    assert m.meta.title
    assert m.meta.source_pdf
    assert m.meta.filled_filename
    assert m.fields


def test_load_mapping_unknown_document():
    with pytest.raises(UnknownDocument):
        _load_mapping("not_a_real_doc")


def test_load_mapping_invalid_json(tmp_path: Path, monkeypatch):
    bad_dir = tmp_path / "mappings"
    bad_dir.mkdir()
    (bad_dir / "broken.json").write_text("{ not valid json")
    monkeypatch.setattr("backend.generate.MAPPINGS_DIR", bad_dir)
    with pytest.raises(InvalidMapping, match="not valid JSON"):
        _load_mapping("broken")


def test_load_mapping_validation_error(tmp_path: Path, monkeypatch):
    """A JSON that parses but is missing required keys should raise InvalidMapping
    with a specific path to the broken field."""
    bad_dir = tmp_path / "mappings"
    bad_dir.mkdir()
    (bad_dir / "missing_meta.json").write_text(json.dumps({"fields": {}}))
    monkeypatch.setattr("backend.generate.MAPPINGS_DIR", bad_dir)
    with pytest.raises(InvalidMapping, match="invalid"):
        _load_mapping("missing_meta")


def test_all_shipped_mappings_validate():
    """Regression guard: every shipped mapping JSON must satisfy MappingFile."""
    for doc_key in ("lease_invoice", "lease_abstract", "tenant_rep", "multiboard"):
        m = _load_mapping(doc_key)
        assert m.fields, f"{doc_key} has no fields"


# ---- /api/generate endpoint tests (real Postgres + auth + filesystem mappings) ----

def _insert_lease_invoice_template_for(user_id: str) -> str:
    """Insert a template row keyed to the existing lease_invoice mapping JSON
    + PDF on disk, so /api/generate has something to fill. Returns the
    template id."""
    from backend import db
    tpl = models.Template(
        id="lease_invoice",
        user_id=user_id,
        title="Lease Invoice",
        source_pdf_path="templates/pdf/2025 Compass Chicagoland Lease Invoice Landlords and Tenant Use copy.pdf",
        mapping_path="backend/mappings/lease_invoice.json",
        status="ready",
        is_default=False,
        extra_fields=[],
        created_at=models.now_iso(),
    )
    with db.get_conn() as conn:
        models.insert_template(conn, tpl)
    return tpl.id


def test_api_generate_partial_success(authed_client):
    """One bad document_key in the batch should NOT 500 — it should land in
    failures while the good docs come through in documents."""
    _insert_lease_invoice_template_for(authed_client.user_id)

    payload = {
        "fields": {
            "transaction_type": "lease",
            "property": {"address": "221 W Hubbard", "unit": "803", "city": "Chicago", "state": "IL", "zip": "60654"},
            "lease_start": "2026-05-04",
            "lease_end": "2027-07-03",
            "monthly_rent": "$3182",
            "tenant_or_buyer_names": ["John Doe"],
            "commission_amount": "$3182",
        },
        "agent": {"name": "Test Agent"},
        "documents": ["lease_invoice", "definitely_not_a_real_doc"],
    }
    r = authed_client.post("/api/generate", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["documents"]) == 1
    assert data["documents"][0]["document"] == "lease_invoice"
    assert len(data["failures"]) == 1
    assert data["failures"][0]["document"] == "definitely_not_a_real_doc"


def test_api_generate_empty_documents_list_is_400(authed_client):
    r = authed_client.post("/api/generate", json={
        "fields": {}, "agent": {}, "documents": [],
    })
    assert r.status_code == 400


def test_api_generate_all_good_docs_no_failures(authed_client):
    _insert_lease_invoice_template_for(authed_client.user_id)
    payload = {
        "fields": {
            "transaction_type": "lease",
            "property": {"address": "221 W Hubbard", "city": "Chicago", "state": "IL", "zip": "60654"},
            "lease_start": "2026-05-04",
            "lease_end": "2027-07-03",
            "monthly_rent": "$3182",
            "tenant_or_buyer_names": ["John Doe"],
        },
        "agent": {"name": "Test Agent"},
        "documents": ["lease_invoice"],
    }
    r = authed_client.post("/api/generate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert len(data["documents"]) == 1
    assert data["failures"] == []


def test_api_generate_other_users_template_lands_in_failures(two_authed_clients):
    """If Bob requests a document_key that exists but belongs to Alice,
    /api/generate must return it as a failure (not 500, not silently
    leaking by filling for him)."""
    alice, bob = two_authed_clients
    _insert_lease_invoice_template_for(alice.user_id)

    payload = {
        "fields": {"transaction_type": "lease"},
        "agent": {"name": "x"},
        "documents": ["lease_invoice"],
    }
    r = bob.post("/api/generate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["documents"] == []
    assert len(data["failures"]) == 1
    assert "not found" in data["failures"][0]["error"]


def test_api_generate_requires_auth(clean_db):
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    r = client.post("/api/generate", json={
        "fields": {}, "agent": {}, "documents": ["lease_invoice"],
    })
    assert r.status_code == 401
