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
    # generated_documents intentionally absent — completed PDFs aren't persisted
    # for privacy; transactions only stores the field snapshot.
    assert {"_migrations", "templates", "transactions"} <= names
    assert "generated_documents" not in names


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


def test_transaction_round_trip(tmp_db: Path):
    """Transactions log the TransactionFields + AgentProfile snapshot per deal.
    Filled PDFs aren't persisted — privacy decision."""
    db.run_migrations(tmp_db)
    txn = models.Transaction(
        id=models.new_id(),
        fields_json='{"property":{"address":"221 W Hubbard"}}',
        agent_json='{"name":"Test Agent"}',
        created_at=models.now_iso(),
    )
    with db.get_conn(tmp_db) as conn:
        models.insert_transaction(conn, txn)
        fetched = models.get_transaction(conn, txn.id)
        listed = models.list_transactions(conn)
    assert fetched is not None
    assert fetched.fields_json == txn.fields_json
    assert fetched.user_id == models.DEFAULT_USER_ID
    assert len(listed) == 1


def test_templates_scoped_per_user(tmp_db: Path):
    """list_templates returns the requesting user's templates plus all defaults
    (which are shared across users). Custom templates from other users stay
    hidden."""
    db.run_migrations(tmp_db)
    alice_tpl = models.Template(
        id=models.new_id(), user_id="alice", title="Alice's Pet Addendum",
        source_pdf_path="custom-pet.pdf", mapping_path="custom-pet.json",
        status="ready", is_default=False,
        created_at=models.now_iso(),
    )
    bob_tpl = models.Template(
        id=models.new_id(), user_id="bob", title="Bob's Pool Disclosure",
        source_pdf_path="bob-pool.pdf", mapping_path="bob-pool.json",
        status="ready", is_default=False,
        created_at=models.now_iso(),
    )
    with db.get_conn(tmp_db) as conn:
        models.insert_template(conn, alice_tpl)
        models.insert_template(conn, bob_tpl)
        alice_view = models.list_templates(conn, user_id="alice")
        bob_view = models.list_templates(conn, user_id="bob")

    alice_titles = {t.title for t in alice_view}
    bob_titles = {t.title for t in bob_view}
    # Both see the 4 IL defaults
    assert "Compass Lease Invoice" in alice_titles
    assert "Compass Lease Invoice" in bob_titles
    # Each only sees their own custom
    assert "Alice's Pet Addendum" in alice_titles
    assert "Alice's Pet Addendum" not in bob_titles
    assert "Bob's Pool Disclosure" in bob_titles
    assert "Bob's Pool Disclosure" not in alice_titles
