"""
Tests for dynamic-schema extraction (Pillar 2 chunk 5) — auth + multi-tenant.

Two layers:
  - build_dynamic_extraction_model: pure Pydantic shape tests
  - /api/extract endpoint: extract_fields mocked, real Postgres + auth cookie
"""
from __future__ import annotations

from backend import models
from backend.extract import build_dynamic_extraction_model
from backend.models import ExtraField
from backend.schema import TransactionFields


# ---- Pure factory tests (no DB, no OpenAI) ----

def test_factory_with_no_templates_returns_base_class():
    assert build_dynamic_extraction_model({}) is TransactionFields


def test_factory_with_empty_extras_lists_returns_base_class():
    Model = build_dynamic_extraction_model({"empty_template": []})
    assert Model is TransactionFields


def test_factory_one_template_adds_template_extras():
    extras = [
        ExtraField(name="pet_name", type="text", description="The pet's name", pdf_field="PET_NAME"),
        ExtraField(name="pet_deposit", type="money", description="Deposit amount", pdf_field="PET_DEPOSIT"),
    ]
    Model = build_dynamic_extraction_model({"pet_addendum": extras})

    assert Model is not TransactionFields
    assert "transaction_type" in Model.model_fields
    assert "property" in Model.model_fields
    assert "template_extras" in Model.model_fields

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
    assert instance.template_extras.pet_addendum.pet_name == "Lucy"
    assert instance.template_extras.pool_disclosure.pool_type == "in-ground"


def test_factory_template_id_with_special_chars_is_sanitized():
    """uuid-style ids contain hyphens which aren't valid Python identifiers.
    The factory should sanitize them so the dynamic class compiles."""
    extras = [ExtraField(name="x", type="text", description="x", pdf_field="X")]
    Model = build_dynamic_extraction_model({"abc-123-def": extras})
    instance = Model.model_validate({
        "template_extras": {"abc_123_def": {"x": "y"}},
    })
    assert instance.template_extras.abc_123_def.x == "y"


def test_factory_extra_field_type_mapping():
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
    """Defense in depth — if ExtraField gains a new type, _TYPE_MAP must
    still cover every value the Literal allows. This catches drift."""
    from typing import get_args
    from backend.extract import _TYPE_MAP
    from backend.models import ExtraFieldType
    declared = set(get_args(ExtraFieldType))
    assert declared.issubset(_TYPE_MAP.keys()), (
        f"_TYPE_MAP is missing entries for: {declared - set(_TYPE_MAP.keys())}"
    )


# ---- Endpoint passthrough tests (auth cookie + real Postgres + mocked OpenAI) ----

def test_extract_endpoint_passes_active_template_extras(authed_client, monkeypatch):
    """Insert a template under alice, hit /api/extract with its id active,
    verify extract_fields received the extras dict."""
    from backend import db, main
    custom = models.Template(
        id="my-custom",
        user_id=authed_client.user_id,
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
    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        captured["notes"] = notes
        captured["template_extras"] = template_extras
        return TransactionFields(transaction_type="lease")

    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = authed_client.post(
        "/api/extract",
        data={"notes": "test notes", "active_template_ids": "my-custom"},
    )
    assert r.status_code == 200, r.text
    assert "my-custom" in captured["template_extras"]
    assert captured["template_extras"]["my-custom"][0].name == "pet_name"


def test_extract_endpoint_ignores_unknown_template_ids(authed_client, monkeypatch):
    """A stale frontend cache might send template ids that no longer exist.
    We silently ignore them — never 400 on the extract path."""
    from backend import main
    captured = {}
    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        captured["template_extras"] = template_extras
        return TransactionFields()

    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = authed_client.post(
        "/api/extract",
        data={"notes": "x", "active_template_ids": "ghost-template-1,ghost-template-2"},
    )
    assert r.status_code == 200
    assert captured["template_extras"] == {}


def test_extract_endpoint_response_is_dict_not_pydantic_model(authed_client, monkeypatch):
    """Endpoint must return a dict so the dynamic shape (with optional
    template_extras key) makes it through FastAPI without response_model
    coercion."""
    from backend import main
    async def fake_extract(notes, images=None, template_extras=None, tier="full"):
        return TransactionFields(transaction_type="lease", monthly_rent="$3000")

    monkeypatch.setattr(main, "extract_fields", fake_extract)

    r = authed_client.post("/api/extract", data={"notes": "test"})
    assert r.status_code == 200
    data = r.json()
    assert data["transaction_type"] == "lease"
    assert data["monthly_rent"] == "$3000"


def test_extract_endpoint_requires_auth(clean_db):
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    r = client.post("/api/extract", data={"notes": "anything"})
    assert r.status_code == 401
