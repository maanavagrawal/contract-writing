"""
Tests for the generate.py refactor + /api/generate per-doc error batching.

Covers:
  - MappingFile validation (load_mapping returns a typed Pydantic model)
  - Invalid JSON → InvalidMapping with a useful message
  - Unknown document_key → UnknownDocument
  - All four shipped mapping JSONs validate against MappingFile (so the
    refactor didn't break the existing happy path)
  - /api/generate returns failures list instead of 500ing
  - /api/generate succeeds partially when one doc is bad and the rest are good
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from backend.generate import InvalidMapping, UnknownDocument, _load_mapping
from backend.main import app
from backend.schema import MappingFile


def test_load_mapping_returns_pydantic_model():
    m = _load_mapping("lease_invoice")
    assert isinstance(m, MappingFile)
    assert m.meta.title  # non-empty
    assert m.meta.source_pdf
    assert m.meta.filled_filename
    assert m.fields  # has at least one mapping entry


def test_load_mapping_unknown_document():
    with pytest.raises(UnknownDocument):
        _load_mapping("not_a_real_doc")


def test_load_mapping_invalid_json(tmp_path: Path, monkeypatch):
    """Point MAPPINGS_DIR at a temp dir with a malformed JSON, expect InvalidMapping."""
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
    """Regression guard: every default mapping JSON must satisfy MappingFile."""
    for doc_key in ("lease_invoice", "lease_abstract", "tenant_rep", "multiboard"):
        m = _load_mapping(doc_key)
        assert m.fields, f"{doc_key} has no fields"


def test_api_generate_partial_success():
    """One bad document_key in the batch should NOT 500 — it should land in
    failures while the good docs come through in documents."""
    client = TestClient(app)
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
    r = client.post("/api/generate", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["documents"]) == 1
    assert data["documents"][0]["document"] == "lease_invoice"
    assert len(data["failures"]) == 1
    assert data["failures"][0]["document"] == "definitely_not_a_real_doc"
    assert "no mapping found" in data["failures"][0]["error"]


def test_api_generate_empty_documents_list_is_400():
    """The one remaining 400: client sent an empty documents array."""
    client = TestClient(app)
    r = client.post("/api/generate", json={
        "fields": {}, "agent": {}, "documents": [],
    })
    assert r.status_code == 400


def test_api_generate_all_good_docs_no_failures():
    """Sanity: a clean batch returns documents and an empty failures list."""
    client = TestClient(app)
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
    r = client.post("/api/generate", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert len(data["documents"]) == 1
    assert data["failures"] == []
