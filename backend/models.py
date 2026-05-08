"""
Database row models + helpers (Pillar 2).

Pydantic models that mirror the sqlite tables. Kept separate from
backend/schema.py to make the boundary clear: schema.py is request/response
shapes; models.py is persistence shapes. They overlap but don't have to —
e.g. extra_fields is JSON-encoded text in the DB and a list of dicts in the
ORM-style model.

Everything here is dataclass-flavored: build from a sqlite3.Row, write back
via repository functions in this module.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

TemplateStatus = Literal["pending_review", "ready", "needs_attention"]
ExtraFieldType = Literal["text", "money", "date", "number", "bool", "list_str"]

# Single-user default until auth ships. When auth lands, every place that
# currently writes DEFAULT_USER swaps to the authenticated user's id.
DEFAULT_USER_ID = "default"


def new_id() -> str:
    """Short uuid4. Stable enough for our scale, no need for ULIDs."""
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ExtraField(BaseModel):
    """One template-specific field that extends the canonical schema for a
    particular template. Stored serialized as JSON in templates.extra_fields."""
    name: str                        # snake_case identifier
    type: ExtraFieldType = "text"
    description: str = ""             # used in extraction prompt
    pdf_field: str = ""               # the AcroForm field name this fills


class Template(BaseModel):
    id: str
    user_id: str = DEFAULT_USER_ID
    title: str
    source_pdf_path: str
    mapping_path: str
    status: TemplateStatus
    is_default: bool
    extra_fields: list[ExtraField] = Field(default_factory=list)
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Template":
        raw_extras = row["extra_fields"]
        try:
            extras = [ExtraField(**e) for e in json.loads(raw_extras or "[]")]
        except (json.JSONDecodeError, TypeError):
            extras = []
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            source_pdf_path=row["source_pdf_path"],
            mapping_path=row["mapping_path"],
            status=row["status"],
            is_default=bool(row["is_default"]),
            extra_fields=extras,
            created_at=row["created_at"],
        )


class Transaction(BaseModel):
    id: str
    user_id: str = DEFAULT_USER_ID
    fields_json: str                  # serialized TransactionFields
    agent_json: str                   # serialized AgentProfile
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Transaction":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            fields_json=row["fields_json"],
            agent_json=row["agent_json"],
            created_at=row["created_at"],
        )


# ---- Repository functions (kept tiny; raw sqlite3 + dict-style row mapping) ----
#
# user_id defaults to DEFAULT_USER_ID throughout. When auth ships, callers
# pass a real authenticated user id and these queries become per-user scoped
# without further changes.

def list_templates(conn: sqlite3.Connection, user_id: str = DEFAULT_USER_ID) -> list[Template]:
    """Return defaults first (is_default=1) then user's own templates by age."""
    rows = conn.execute(
        """
        SELECT * FROM templates
        WHERE user_id = ? OR is_default = 1
        ORDER BY is_default DESC, created_at ASC
        """,
        (user_id,),
    ).fetchall()
    return [Template.from_row(r) for r in rows]


def get_template(conn: sqlite3.Connection, tpl_id: str) -> Template | None:
    row = conn.execute("SELECT * FROM templates WHERE id = ?", (tpl_id,)).fetchone()
    return Template.from_row(row) if row else None


def insert_template(conn: sqlite3.Connection, tpl: Template) -> None:
    conn.execute(
        """
        INSERT INTO templates
            (id, user_id, title, source_pdf_path, mapping_path, status, is_default, extra_fields, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tpl.id, tpl.user_id, tpl.title, tpl.source_pdf_path, tpl.mapping_path,
            tpl.status, int(tpl.is_default),
            json.dumps([e.model_dump() for e in tpl.extra_fields]),
            tpl.created_at,
        ),
    )


def update_template_status(conn: sqlite3.Connection, tpl_id: str, status: TemplateStatus) -> None:
    conn.execute("UPDATE templates SET status = ? WHERE id = ?", (status, tpl_id))


def delete_template(conn: sqlite3.Connection, tpl_id: str) -> None:
    """Refuses to delete a default. Caller should check is_default first; this
    is a defense-in-depth check."""
    conn.execute("DELETE FROM templates WHERE id = ? AND is_default = 0", (tpl_id,))


def insert_transaction(conn: sqlite3.Connection, txn: Transaction) -> None:
    conn.execute(
        """
        INSERT INTO transactions (id, user_id, fields_json, agent_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (txn.id, txn.user_id, txn.fields_json, txn.agent_json, txn.created_at),
    )


def get_transaction(conn: sqlite3.Connection, txn_id: str) -> Transaction | None:
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return Transaction.from_row(row) if row else None


def list_transactions(conn: sqlite3.Connection, user_id: str = DEFAULT_USER_ID) -> list[Transaction]:
    """Per-deal history for a user. Used to scan past deals by created_at;
    the actual filled PDFs aren't stored, only the TransactionFields snapshot."""
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [Transaction.from_row(r) for r in rows]
