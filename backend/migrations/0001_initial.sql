-- Initial schema for Pillar 2.
--
-- templates: every PDF the agent can fill from. Default IL templates are
--   seeded in 0002 and marked is_default=1 so they can't be deleted.
--   extra_fields is a JSON array of {name,type,description,pdf_field} that
--   extends TransactionFields when this template is active for extraction.
--
-- transactions + generated_documents: persist every fill so future DocuSign
--   integration can re-send / re-issue without re-running extraction.

CREATE TABLE IF NOT EXISTS templates (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    source_pdf_path TEXT NOT NULL,
    mapping_path    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending_review', 'ready', 'needs_attention')),
    is_default      INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    extra_fields    TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id          TEXT PRIMARY KEY,
    fields_json TEXT NOT NULL,
    agent_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS generated_documents (
    id             TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    template_id    TEXT NOT NULL REFERENCES templates(id) ON DELETE RESTRICT,
    pdf_path       TEXT NOT NULL,
    filename       TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_generated_documents_txn
    ON generated_documents(transaction_id);
