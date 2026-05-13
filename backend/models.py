"""
Database row models + helpers (Pillar 2 / multi-tenant).

Pydantic models that mirror the Postgres tables. Kept separate from
backend/schema.py to make the boundary clear: schema.py is request/response
shapes; models.py is persistence shapes. They overlap but don't have to —
e.g. extra_fields is JSON-encoded text in the DB and a list of dicts in the
ORM-style model.

Repository functions take a psycopg connection and a user_id. user_id is
required everywhere — there is no "shared default" in multi-tenant mode.
A query that forgets to pass user_id is the kind of bug that leaks user A's
templates to user B; the type signature makes that mistake hard to make.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

import psycopg
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
    name: str
    type: ExtraFieldType = "text"
    description: str = ""
    pdf_field: str = ""


class Template(BaseModel):
    id: str
    user_id: str
    title: str
    source_pdf_path: str
    mapping_path: str
    status: TemplateStatus
    is_default: bool = False
    extra_fields: list[ExtraField] = Field(default_factory=list)
    created_at: str
    # SHA-256 of the uploaded PDF bytes. Cache key for the AI mapping: if a
    # user re-uploads the same PDF we copy the existing mapping; if they
    # upload a revision (v8.0 -> v8.1) the hash differs and we re-map.
    # Nullable for rows inserted before migration 0004.
    pdf_sha256: str | None = None

    @classmethod
    def from_row(cls, row: tuple) -> "Template":
        # Column order matches the SELECT in _TEMPLATES_COLUMNS below.
        (id_, user_id, title, source_pdf_path, mapping_path,
         status, is_default, extra_fields_raw, created_at, pdf_sha256) = row
        try:
            extras = [ExtraField(**e) for e in json.loads(extra_fields_raw or "[]")]
        except (json.JSONDecodeError, TypeError):
            extras = []
        return cls(
            id=id_,
            user_id=user_id,
            title=title,
            source_pdf_path=source_pdf_path,
            mapping_path=mapping_path,
            status=status,
            is_default=bool(is_default),
            extra_fields=extras,
            created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
            pdf_sha256=pdf_sha256,
        )


class Transaction(BaseModel):
    id: str
    user_id: str
    fields_json: str
    agent_json: str
    created_at: str

    @classmethod
    def from_row(cls, row: tuple) -> "Transaction":
        id_, user_id, fields_json, agent_json, created_at = row
        return cls(
            id=id_,
            user_id=user_id,
            fields_json=fields_json,
            agent_json=agent_json,
            created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        )


# ---- Repository functions ----
#
# Every read and write is scoped to a user_id. Defaults intentionally aren't
# provided so a forgotten parameter raises TypeError instead of silently
# leaking data across tenants.

_TEMPLATES_COLUMNS = (
    "id, user_id, title, source_pdf_path, mapping_path, "
    "status, is_default, extra_fields, created_at, pdf_sha256"
)


def list_templates(conn: psycopg.Connection, user_id: str) -> list[Template]:
    """Return the user's own templates, oldest first."""
    rows = conn.execute(
        f"""
        SELECT {_TEMPLATES_COLUMNS} FROM templates
        WHERE user_id = %s
        ORDER BY created_at ASC
        """,
        (user_id,),
    ).fetchall()
    return [Template.from_row(r) for r in rows]


def get_template(
    conn: psycopg.Connection,
    tpl_id: str,
    user_id: str,
) -> Template | None:
    """Fetch a template by id, scoped to the caller's user_id. Returns None
    for templates the caller doesn't own — never 'exists but forbidden',
    so attackers can't probe id-space to enumerate other users' template ids
    (no IDOR signal)."""
    row = conn.execute(
        f"SELECT {_TEMPLATES_COLUMNS} FROM templates WHERE id = %s AND user_id = %s",
        (tpl_id, user_id),
    ).fetchone()
    return Template.from_row(row) if row else None


def insert_template(conn: psycopg.Connection, tpl: Template) -> None:
    conn.execute(
        """
        INSERT INTO templates
            (id, user_id, title, source_pdf_path, mapping_path, status,
             is_default, extra_fields, created_at, pdf_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tpl.id, tpl.user_id, tpl.title, tpl.source_pdf_path, tpl.mapping_path,
            tpl.status, tpl.is_default,
            json.dumps([e.model_dump() for e in tpl.extra_fields]),
            tpl.created_at,
            tpl.pdf_sha256,
        ),
    )


def find_template_by_pdf_sha(
    conn: psycopg.Connection,
    pdf_sha256: str,
    user_id: str,
) -> Template | None:
    """Look up an existing template by PDF hash, scoped to the caller. Used
    at upload time to skip the AI mapping call when the same user uploads
    the same PDF bytes twice. Cross-user matches are not used — Alice's
    cached mapping is private to Alice.

    Note: deliberately does NOT filter by status. Even templates flagged
    `needs_attention` have a usable mapping (the AI generated something);
    the flag just means the user might want to review it. Skipping the
    AI call on re-upload is the right behavior regardless of flag state —
    we'd just re-generate the same mapping (and the same flag) for $$$.
    """
    row = conn.execute(
        f"""
        SELECT {_TEMPLATES_COLUMNS} FROM templates
        WHERE pdf_sha256 = %s AND user_id = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (pdf_sha256, user_id),
    ).fetchone()
    return Template.from_row(row) if row else None


def update_template_title(
    conn: psycopg.Connection,
    tpl_id: str,
    user_id: str,
    title: str,
) -> int:
    """Update the title on a template the caller owns. Returns the number of
    rows affected so callers can detect "not yours / not found" without
    leaking that distinction to the API."""
    cur = conn.execute(
        "UPDATE templates SET title = %s WHERE id = %s AND user_id = %s",
        (title, tpl_id, user_id),
    )
    return cur.rowcount


def update_template_extra_fields(
    conn: psycopg.Connection,
    tpl_id: str,
    user_id: str,
    extras: list[ExtraField],
) -> int:
    """Replace the extra_fields list for a template. Used by the PATCH
    /api/templates/<id>/mapping endpoint when the user registers a new
    template-specific field via the low-confidence review UI. Without
    this DB write, the mapping JSON's extra_fields would diverge from
    the templates.extra_fields column — /api/extract reads from the DB
    when building the dynamic Pydantic schema, so an extra that lives
    only in the JSON never reaches the AI and the corresponding
    {template_extras.X} reference would always render blank.

    Returns rowcount so the caller can detect missing/cross-user rows."""
    cur = conn.execute(
        "UPDATE templates SET extra_fields = %s WHERE id = %s AND user_id = %s",
        (json.dumps([e.model_dump() for e in extras]), tpl_id, user_id),
    )
    return cur.rowcount


def update_template_status(
    conn: psycopg.Connection,
    tpl_id: str,
    user_id: str,
    status: TemplateStatus,
) -> None:
    conn.execute(
        "UPDATE templates SET status = %s WHERE id = %s AND user_id = %s",
        (status, tpl_id, user_id),
    )


def delete_template(
    conn: psycopg.Connection,
    tpl_id: str,
    user_id: str,
) -> int:
    """Delete a template by id, scoped to the caller's user_id. Returns the
    number of rows affected so callers can distinguish 'not found / not yours'
    from 'deleted'. No special handling for is_default — every template is
    user-owned in multi-tenant mode."""
    cur = conn.execute(
        "DELETE FROM templates WHERE id = %s AND user_id = %s",
        (tpl_id, user_id),
    )
    return cur.rowcount


def insert_transaction(conn: psycopg.Connection, txn: Transaction) -> None:
    conn.execute(
        """
        INSERT INTO transactions (id, user_id, fields_json, agent_json, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (txn.id, txn.user_id, txn.fields_json, txn.agent_json, txn.created_at),
    )


def get_transaction(
    conn: psycopg.Connection,
    txn_id: str,
    user_id: str,
) -> Transaction | None:
    row = conn.execute(
        "SELECT id, user_id, fields_json, agent_json, created_at "
        "FROM transactions WHERE id = %s AND user_id = %s",
        (txn_id, user_id),
    ).fetchone()
    return Transaction.from_row(row) if row else None


def list_transactions(conn: psycopg.Connection, user_id: str) -> list[Transaction]:
    """Per-deal history for a user. Used to scan past deals by created_at;
    the actual filled PDFs aren't stored, only the TransactionFields snapshot."""
    rows = conn.execute(
        "SELECT id, user_id, fields_json, agent_json, created_at "
        "FROM transactions WHERE user_id = %s ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [Transaction.from_row(r) for r in rows]
