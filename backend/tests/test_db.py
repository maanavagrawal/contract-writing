"""
Tests for the sqlite + migrations layer.

Each test points the runner at a temp DB so we don't poison data/app.sqlite.
We exercise: clean migrate, idempotent re-migrate, schema shape, seed
correctness, and the repository read path.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Tests don't actually call OpenAI but extract.py loads at module import.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from backend import db, models


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Fresh sqlite in a per-test temp dir."""
    return tmp_path / "test.sqlite"


def test_migrations_apply_cleanly(tmp_db: Path):
    applied = db.run_migrations(tmp_db)
    # 0001_initial + 0002_seed_default_templates expected.
    assert "0001_initial" in applied
    assert "0002_seed_default_templates" in applied
    assert tmp_db.exists()


def test_migrations_are_idempotent(tmp_db: Path):
    db.run_migrations(tmp_db)
    second = db.run_migrations(tmp_db)
    # Second run should be a no-op.
    assert second == []


def test_schema_has_expected_tables(tmp_db: Path):
    db.run_migrations(tmp_db)
    with db.get_conn(tmp_db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
    # _migrations is internal bookkeeping; the rest are the Pillar 2 tables.
    assert {"_migrations", "templates", "transactions", "generated_documents"} <= names


def test_seed_inserts_four_il_defaults(tmp_db: Path):
    db.run_migrations(tmp_db)
    with db.get_conn(tmp_db) as conn:
        templates = models.list_templates(conn)
    ids = {t.id for t in templates}
    # The 4 mapping JSONs in backend/mappings/ all have matching PDFs in
    # templates/pdf/, so all four should seed.
    assert ids == {"lease_invoice", "lease_abstract", "tenant_rep", "multiboard"}
    for t in templates:
        assert t.is_default is True
        assert t.status == "ready"
        assert t.extra_fields == []
        assert t.title  # non-empty


def test_template_round_trip(tmp_db: Path):
    """Insert a non-default custom template, read it back, verify shape."""
    db.run_migrations(tmp_db)
    custom = models.Template(
        id=models.new_id(),
        title="Pet Addendum",
        source_pdf_path="templates/pdf/custom-pet.pdf",
        mapping_path="backend/mappings/custom-pet.json",
        status="pending_review",
        is_default=False,
        extra_fields=[
            models.ExtraField(name="pet_name", type="text", description="Pet's name", pdf_field="PET_NAME"),
        ],
        created_at=models.now_iso(),
    )
    with db.get_conn(tmp_db) as conn:
        models.insert_template(conn, custom)
        fetched = models.get_template(conn, custom.id)
    assert fetched is not None
    assert fetched.title == "Pet Addendum"
    assert fetched.is_default is False
    assert len(fetched.extra_fields) == 1
    assert fetched.extra_fields[0].name == "pet_name"


def test_default_template_cannot_be_deleted(tmp_db: Path):
    """delete_template has a WHERE is_default = 0 guard. Default rows survive."""
    db.run_migrations(tmp_db)
    with db.get_conn(tmp_db) as conn:
        models.delete_template(conn, "lease_invoice")
        still_there = models.get_template(conn, "lease_invoice")
    assert still_there is not None


def test_foreign_keys_enforced(tmp_db: Path):
    """generated_documents.transaction_id has ON DELETE CASCADE; the FK pragma
    has to be on for that to fire. Verifies _connect sets it."""
    import sqlite3
    db.run_migrations(tmp_db)
    txn = models.Transaction(
        id=models.new_id(), fields_json="{}", agent_json="{}",
        created_at=models.now_iso(),
    )
    doc = models.GeneratedDocument(
        id=models.new_id(), transaction_id=txn.id, template_id="lease_invoice",
        pdf_path="x.pdf", filename="x.pdf", created_at=models.now_iso(),
    )
    with db.get_conn(tmp_db) as conn:
        models.insert_transaction(conn, txn)
        models.insert_generated_document(conn, doc)
        # Pointing at a nonexistent template should fail FK check.
        bad = doc.model_copy(update={"id": models.new_id(), "template_id": "does-not-exist"})
        with pytest.raises(sqlite3.IntegrityError):
            models.insert_generated_document(conn, bad)
