"""
Tests for the Postgres + migrations layer.

The conftest fixtures handle:
  - postgres_container (session-scoped) — one container for all tests
  - clean_db (function-scoped) — TRUNCATE before each test for isolation

We exercise: clean migrate, idempotent re-migrate, schema shape, repository
read/write paths, and the per-user privacy boundary that's now load-bearing
(no shared defaults — every template belongs to exactly one user).
"""
from __future__ import annotations

import pytest

from backend import db, models


def test_migrations_apply_cleanly(postgres_container, clean_db):
    """Re-running migrations against an already-migrated DB should be a no-op
    because every applied id is recorded in _migrations."""
    applied = db.run_migrations()
    # Already applied during the session fixture's setup.
    assert applied == []


def test_migrations_are_idempotent(postgres_container, clean_db):
    first = db.run_migrations()
    second = db.run_migrations()
    assert first == []
    assert second == []


def test_schema_has_expected_tables(postgres_container, clean_db):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()
    names = {r[0] for r in rows}
    # _migrations is internal bookkeeping; templates + transactions are the
    # original Pillar 2 tables; users + sessions are auth.
    assert {"_migrations", "templates", "transactions", "users", "sessions"} <= names


def test_template_round_trip(postgres_container, clean_db):
    """Insert a custom template, read it back, verify shape including extras."""
    custom = models.Template(
        id=models.new_id(),
        user_id="alice",
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
    with db.get_conn() as conn:
        models.insert_template(conn, custom)
        fetched = models.get_template(conn, custom.id, user_id="alice")
    assert fetched is not None
    assert fetched.title == "Pet Addendum"
    assert fetched.user_id == "alice"
    assert fetched.is_default is False
    assert len(fetched.extra_fields) == 1
    assert fetched.extra_fields[0].name == "pet_name"


def test_get_template_returns_none_across_user_boundary(postgres_container, clean_db):
    """The IDOR guard. Bob queries Alice's template id → None, NOT a 'forbidden'
    signal. Returning None for both 'doesn't exist' and 'not yours' means an
    attacker can't enumerate template ids."""
    alice_tpl = models.Template(
        id=models.new_id(),
        user_id="alice",
        title="Alice's Pet Addendum",
        source_pdf_path="custom-pet.pdf",
        mapping_path="custom-pet.json",
        status="ready",
        created_at=models.now_iso(),
    )
    with db.get_conn() as conn:
        models.insert_template(conn, alice_tpl)
        bob_view = models.get_template(conn, alice_tpl.id, user_id="bob")
    assert bob_view is None


def test_list_templates_only_returns_caller_templates(postgres_container, clean_db):
    """list_templates is the only place a missed user_id check would leak data
    in bulk. Each user sees only their own templates — no shared rows."""
    alice_tpl = models.Template(
        id=models.new_id(), user_id="alice", title="Alice's Pet Addendum",
        source_pdf_path="custom-pet.pdf", mapping_path="custom-pet.json",
        status="ready", created_at=models.now_iso(),
    )
    bob_tpl = models.Template(
        id=models.new_id(), user_id="bob", title="Bob's Pool Disclosure",
        source_pdf_path="bob-pool.pdf", mapping_path="bob-pool.json",
        status="ready", created_at=models.now_iso(),
    )
    with db.get_conn() as conn:
        models.insert_template(conn, alice_tpl)
        models.insert_template(conn, bob_tpl)
        alice_view = models.list_templates(conn, user_id="alice")
        bob_view = models.list_templates(conn, user_id="bob")

    assert {t.title for t in alice_view} == {"Alice's Pet Addendum"}
    assert {t.title for t in bob_view} == {"Bob's Pool Disclosure"}


def test_delete_template_cannot_cross_user_boundary(postgres_container, clean_db):
    """delete_template scoped to user_id; Bob's DELETE on Alice's template
    affects zero rows."""
    alice_tpl = models.Template(
        id=models.new_id(), user_id="alice", title="t",
        source_pdf_path="x.pdf", mapping_path="x.json",
        status="ready", created_at=models.now_iso(),
    )
    with db.get_conn() as conn:
        models.insert_template(conn, alice_tpl)
        rowcount = models.delete_template(conn, alice_tpl.id, user_id="bob")
        # Alice's template still there
        still_there = models.get_template(conn, alice_tpl.id, user_id="alice")
    assert rowcount == 0
    assert still_there is not None


def test_delete_template_owner_succeeds(postgres_container, clean_db):
    alice_tpl = models.Template(
        id=models.new_id(), user_id="alice", title="t",
        source_pdf_path="x.pdf", mapping_path="x.json",
        status="ready", created_at=models.now_iso(),
    )
    with db.get_conn() as conn:
        models.insert_template(conn, alice_tpl)
        rowcount = models.delete_template(conn, alice_tpl.id, user_id="alice")
        gone = models.get_template(conn, alice_tpl.id, user_id="alice")
    assert rowcount == 1
    assert gone is None


def test_transaction_round_trip(postgres_container, clean_db):
    """Transactions log the TransactionFields + AgentProfile snapshot per deal.
    Filled PDFs aren't persisted — privacy decision."""
    txn = models.Transaction(
        id=models.new_id(),
        user_id="alice",
        fields_json='{"property":{"address":"221 W Hubbard"}}',
        agent_json='{"name":"Test Agent"}',
        created_at=models.now_iso(),
    )
    with db.get_conn() as conn:
        models.insert_transaction(conn, txn)
        fetched = models.get_transaction(conn, txn.id, user_id="alice")
        listed = models.list_transactions(conn, user_id="alice")
        # Cross-user fetch → None
        bob_view = models.get_transaction(conn, txn.id, user_id="bob")
    assert fetched is not None
    assert fetched.fields_json == txn.fields_json
    assert fetched.user_id == "alice"
    assert len(listed) == 1
    assert bob_view is None
