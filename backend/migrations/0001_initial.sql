-- Initial schema (Postgres).
--
-- templates: every PDF an agent can fill from. Each row is owned by one user;
--   uploads create rows scoped to the uploader's user_id. extra_fields is a
--   JSON array of {name,type,description,pdf_field} that extends
--   TransactionFields when this template is active for extraction.
--
-- transactions: per-deal history. Logs the TransactionFields snapshot used
--   to fill, so the agent can scan past deals by address without storing
--   the actual filled PDFs. Generated PDFs only round-trip as base64 in
--   the API response — never written to disk, never persisted in the DB.
--
-- is_default exists for back-compat with code paths that still reference
-- the column; defaults to FALSE and is no longer special-cased in queries.
-- Old per-tenant code asserted defaults were shared across users; multi-tenancy
-- requires every template be scoped to a single user_id.

CREATE TABLE IF NOT EXISTS templates (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    title           TEXT NOT NULL,
    source_pdf_path TEXT NOT NULL,
    mapping_path    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending_review', 'ready', 'needs_attention')),
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    extra_fields    TEXT NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_templates_user
    ON templates(user_id);

CREATE TABLE IF NOT EXISTS transactions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    agent_json  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_user
    ON transactions(user_id);
