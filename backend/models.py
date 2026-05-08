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
    fields_json: str                  # serialized TransactionFields
    agent_json: str                   # serialized AgentProfile
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Transaction":
        return cls(
            id=row["id"],
            fields_json=row["fields_json"],
            agent_json=row["agent_json"],
            created_at=row["created_at"],
        )


class GeneratedDocument(BaseModel):
    id: str
    transaction_id: str
    template_id: str
    pdf_path: str
    filename: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GeneratedDocument":
        return cls(
            id=row["id"],
            transaction_id=row["transaction_id"],
            template_id=row["template_id"],
            pdf_path=row["pdf_path"],
            filename=row["filename"],
            created_at=row["created_at"],
        )


# ---- Repository functions (kept tiny; raw sqlite3 + dict-style row mapping) ----

def list_templates(conn: sqlite3.Connection) -> list[Template]:
    rows = conn.execute(
        "SELECT * FROM templates ORDER BY is_default DESC, created_at ASC"
    ).fetchall()
    return [Template.from_row(r) for r in rows]


def get_template(conn: sqlite3.Connection, tpl_id: str) -> Template | None:
    row = conn.execute("SELECT * FROM templates WHERE id = ?", (tpl_id,)).fetchone()
    return Template.from_row(row) if row else None


def insert_template(conn: sqlite3.Connection, tpl: Template) -> None:
    conn.execute(
        """
        INSERT INTO templates
            (id, title, source_pdf_path, mapping_path, status, is_default, extra_fields, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tpl.id, tpl.title, tpl.source_pdf_path, tpl.mapping_path,
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
        INSERT INTO transactions (id, fields_json, agent_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (txn.id, txn.fields_json, txn.agent_json, txn.created_at),
    )


def insert_generated_document(conn: sqlite3.Connection, doc: GeneratedDocument) -> None:
    conn.execute(
        """
        INSERT INTO generated_documents
            (id, transaction_id, template_id, pdf_path, filename, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            doc.id, doc.transaction_id, doc.template_id,
            doc.pdf_path, doc.filename, doc.created_at,
        ),
    )


def get_transaction(conn: sqlite3.Connection, txn_id: str) -> Transaction | None:
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return Transaction.from_row(row) if row else None


def list_documents_for_transaction(conn: sqlite3.Connection, txn_id: str) -> list[GeneratedDocument]:
    rows = conn.execute(
        "SELECT * FROM generated_documents WHERE transaction_id = ? ORDER BY created_at ASC",
        (txn_id,),
    ).fetchall()
    return [GeneratedDocument.from_row(r) for r in rows]
