-- Initial schema for Pillar 2.
--
-- templates: every PDF an agent can fill from. Default IL templates are
--   seeded in 0002 and marked is_default=1 so they can't be deleted.
--   extra_fields is a JSON array of {name,type,description,pdf_field} that
--   extends TransactionFields when this template is active for extraction.
--   user_id is 'default' for now (single-user); when auth ships, real
--   identities take over without a schema rewrite.
--
-- transactions: per-deal history. Logs the TransactionFields snapshot used
--   to fill, so the agent can scan past deals by address without storing
--   the actual filled PDFs. Generated PDFs only round-trip as base64 in
--   the API response — never written to disk, never persisted in the DB.

CREATE TABLE IF NOT EXISTS templates (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default',
    title           TEXT NOT NULL,
    source_pdf_path TEXT NOT NULL,
    mapping_path    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending_review', 'ready', 'needs_attention')),
    is_default      INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    extra_fields    TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_templates_user
    ON templates(user_id);

CREATE TABLE IF NOT EXISTS transactions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'default',
    fields_json TEXT NOT NULL,
    agent_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_transactions_user
    ON transactions(user_id);
