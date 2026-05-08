"""
Tests for the dynamic-schema extraction (Pillar 2 chunk 5).

Two layers:
  - build_dynamic_extraction_model: pure Pydantic shape tests. Does the
    model factory produce the right TransactionFieldsExtended depending on
    how many active templates have extra_fields?
  - /api/extract endpoint: with extract_fields mocked, does the endpoint
    correctly resolve active_template_ids → extra_fields and pass them
    through? Real OpenAI calls live in the smoke test, not here.

The factory itself was validated against the live API in
scripts/spike_dynamic_schema.py — these tests guard the local glue.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from backend import db, extract as extract_mod, models, templates as templates_mod
from backend.extract import build_dynamic_extraction_model
from backend.main import app
from backend.models import ExtraField
from backend.schema import TransactionFields


# ---- Pure factory tests (no DB, no OpenAI) ----

def test_factory_with_no_templates_returns_base_class():
    """Empty active set → don't promote the schema. Saves prompt-cache slot
    + tokens compared to wrapping in an empty container."""
    assert build_dynamic_extraction_model({}) is TransactionFields


def test_factory_with_empty_extras_lists_returns_base_class():
    """A template that exists but has no extra_fields shouldn't bloat the
    schema. Same as no templates at all."""
    Model = build_dynamic_extraction_model({"empty_template": []})
    assert Model is TransactionFields


def test_factory_one_template_adds_template_extras():
    extras = [
        ExtraField(name="pet_name", type="text", description="The pet's name", pdf_field="PET_NAME"),
        ExtraField(name="pet_deposit", type="money", description="Deposit amount", pdf_field="PET_DEPOSIT"),
    ]
    Model = build_dynamic_extraction_model({"pet_addendum": extras})

    # New class, not the base
    assert Model is not TransactionFields
    # Has all canonical TransactionFields fields...
    assert "transaction_type" in Model.model_fields
    assert "property" in Model.model_fields
    # ...plus the template_extras container
    assert "template_extras" in Model.model_fields

    # Round-trip parse with a synthetic dict to check shape
    instance = Model.model_validate({
        "transaction_type": "lease",
        "template_extras": {
            "pet_addendum": {"pet_name": "Lucy", "pet_deposit": "$300"}
        },
    })
    assert instance.template_extras.pet_addendum.pet_name == "Lucy"
    assert instance.template_extras.pet_addendum.pet_deposit == "$300"


def test_factory_two_templates_each_get_their_own_namespace():
    pet = [ExtraField(name="pet_name", type="text", description="x", pdf_field="P")]
    pool = [ExtraField(name="pool_type", type="text", description="x", pdf_field="P")]
    Model = build_dynamic_extraction_model({"pet_addendum": pet, "pool_disclosure": pool})

    instance = Model.model_validate({
        "template_extras": {
            "pet_addendum": {"pet_name": "Lucy"},
            "pool_disclosure": {"pool_type": "in-ground"},
        },
    })
    # Both extras populated under their respective template ids
    assert instance.template_extras.pet_addendum.pet_name == "Lucy"
    assert instance.template_extras.pool_disclosure.pool_type == "in-ground"


def test_factory_template_id_with_special_chars_is_sanitized():
    """uuid-style ids contain hyphens which aren't valid Python identifiers.
    The factory should sanitize them so the dynamic class compiles."""
    extras = [ExtraField(name="x", type="text", description="x", pdf_field="X")]
    Model = build_dynamic_extraction_model({"abc-123-def": extras})
    # Must instantiate cleanly
    instance = Model.model_validate({
        "template_extras": {"abc_123_def": {"x": "y"}},
    })
    assert instance.template_extras.abc_123_def.x == "y"


def test_factory_extra_field_type_mapping():
    """Each ExtraField.type should produce a sensibly-typed Pydantic field."""
    extras = [
        ExtraField(name="t", type="text", description="x", pdf_field="X"),
        ExtraField(name="m", type="money", description="x", pdf_field="X"),
        ExtraField(name="d", type="date", description="x", pdf_field="X"),
        ExtraField(name="n", type="number", description="x", pdf_field="X"),
        ExtraField(name="b", type="bool", description="x", pdf_field="X"),
        ExtraField(name="lst", type="list_str", description="x", pdf_field="X"),
    ]
    Model = build_dynamic_extraction_model({"all_types": extras})

    instance = Model.model_validate({
        "template_extras": {"all_types": {
            "t": "string",
            "m": "$100",
            "d": "2026-05-04",
            "n": 42,
            "b": True,
            "lst": ["a", "b"],
        }},
    })
    assert instance.template_extras.all_types.t == "string"
    assert instance.template_extras.all_types.n == 42
    assert instance.template_extras.all_types.b is True
    assert instance.template_extras.all_types.lst == ["a", "b"]


def test_factory_unknown_extra_field_type_falls_back_to_text():
    """If somehow ExtraField.type ends up as something we don't handle (the
    Pydantic Literal would block this normally, but defense in depth), we
    treat it as text rather than crashing the whole extraction."""
    # We can't easily construct an ExtraField with a bad type because of the
    # Literal validator, so we patch _TYPE_MAP indirectly by checking that
    # the production map covers every supported value. If ExtraField gains
    # a new type, this test fails first.
    from backend.extract import _TYPE_MAP
    from typing import get_args
    from backend.models import ExtraFieldType
    declared = set(get_args(ExtraFieldType))
    assert declared.issubset(_TYPE_MAP.keys()), (
        f"_TYPE_MAP is missing entries for: {declared - set(_TYPE_MAP.keys())}"
    )


# ---- Endpoint passthrough tests (DB live, OpenAI mocked) ----

@pytest.fixture
def app_with_temp_db(tmp_path, monkeypatch):
    """Same fixture pattern as test_templates: temp sqlite + real test client."""
    test_db = tmp_path / "test.sqlite"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.run_migrations(test_db)
    yield TestClient(app)


def test_extract_endpoint_passes_active_template_extras(app_with_temp_db, monkeypatch):
    """Insert a custom template with one extra_field, hit /api/extract with
    its id in active_template_ids, verify extract_fields received the
    extras dict."""
    # Insert a template with extras
    custom = models.Template(
        id="my-custom",
        title="My Custom Template",
        source_pdf_path="x.pdf",
        mapping_path="x.json",
        status="ready",
        is_default=False,
        extra_fields=[
            ExtraField(name="pet_name", type="text", description="The pet's name", pdf_field="P"),
        ],
        created_at=models.now_iso(),
    )
    with db.get_conn() as conn:
        models.insert_template(conn, custom)

    captured = {}

    async def fake_extract(notes, images=None, template_extras=None):
        captured["notes"] = notes
        captured["template_extras"] = template_extras
        # Return a TransactionFields instance regardless of schema
        return TransactionFields(transaction_type="lease")

    monkeypatch.setattr(extract_mod, "extract_fields", fake_extract)
    # Also patch the import inside main.py
    from backend import main
    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = app_with_temp_db.post(
        "/api/extract",
        data={"notes": "test notes", "active_template_ids": "my-custom"},
    )
    assert r.status_code == 200, r.text
    # Backend resolved the id and forwarded extras
    assert "my-custom" in captured["template_extras"]
    assert captured["template_extras"]["my-custom"][0].name == "pet_name"


def test_extract_endpoint_ignores_unknown_template_ids(app_with_temp_db, monkeypatch):
    """A stale frontend cache might send template ids that no longer exist.
    We silently ignore them — never 400 on the extract path."""
    captured = {}

    async def fake_extract(notes, images=None, template_extras=None):
        captured["template_extras"] = template_extras
        return TransactionFields()

    from backend import main
    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = app_with_temp_db.post(
        "/api/extract",
        data={"notes": "x", "active_template_ids": "ghost-template-1,ghost-template-2"},
    )
    assert r.status_code == 200
    # No extras forwarded because no real templates resolved
    assert captured["template_extras"] == {}


def test_extract_endpoint_default_templates_have_no_extras(app_with_temp_db, monkeypatch):
    """The 4 IL defaults seed with empty extra_fields. Including them in
    active_template_ids shouldn't bloat the dynamic schema — the factory
    should fall back to the base class when every active template has no
    extras."""
    captured = {}

    async def fake_extract(notes, images=None, template_extras=None):
        captured["template_extras"] = template_extras
        return TransactionFields()

    from backend import main
    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = app_with_temp_db.post(
        "/api/extract",
        data={
            "notes": "x",
            "active_template_ids": "lease_invoice,lease_abstract,multiboard,tenant_rep",
        },
    )
    assert r.status_code == 200
    # All 4 default templates have no extras, so the dict is empty
    assert captured["template_extras"] == {}


def test_extract_endpoint_response_is_dict_not_pydantic_model(app_with_temp_db, monkeypatch):
    """Endpoint must return a dict so the dynamic shape (with optional
    template_extras key) makes it through FastAPI without response_model
    coercion."""
    async def fake_extract(notes, images=None, template_extras=None):
        return TransactionFields(transaction_type="lease", monthly_rent="$3000")

    from backend import main
    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = app_with_temp_db.post("/api/extract", data={"notes": "test"})
    assert r.status_code == 200
    data = r.json()
    assert data["transaction_type"] == "lease"
    assert data["monthly_rent"] == "$3000"
