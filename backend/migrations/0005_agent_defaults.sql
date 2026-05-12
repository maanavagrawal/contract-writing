-- Per-agent defaults: values that rarely change across deals (escrowee,
-- loan_amortization_years, brokerage info, etc.) and should auto-prefill on
-- new transactions. Allow-list is enforced in the API layer, not the DB,
-- so adding/removing eligible paths doesn't require a migration.
--
-- PK is (user_id, field_path) so PUT is a natural UPSERT and an agent can
-- never collide with another agent's row. ON DELETE CASCADE so removing a
-- user wipes their defaults cleanly.

CREATE TABLE IF NOT EXISTS agent_defaults (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field_path  TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, field_path)
);

CREATE INDEX IF NOT EXISTS idx_agent_defaults_user
    ON agent_defaults(user_id);
