-- Global magic-link send rate limit.
--
-- Per-user rate limit (users.token_request_count) protects existing accounts
-- from spam, but doesn't help when an attacker hits /api/auth/login with a
-- stream of unique new emails — each one hits the unknown-user path and gets
-- its own fresh counter.
--
-- This table is the global backstop: a single row, atomically incremented on
-- every magic-link send. When the count crosses GLOBAL_RATE_LIMIT_MAX_SENDS
-- in the rolling window, all sends are refused regardless of the email.
--
-- Bound is intentionally generous (default 50/hour) so two real users
-- requesting links on the same morning aren't blocked. The cap exists to
-- catch automated abuse, not gate normal usage.
--
-- Single row keyed on a constant id so we never need WHERE/INSERT logic;
-- the row is created up-front and only ever UPDATEd.

CREATE TABLE IF NOT EXISTS auth_rate_limit (
    id            TEXT PRIMARY KEY,
    send_count    INTEGER NOT NULL DEFAULT 0,
    window_start  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO auth_rate_limit (id, send_count, window_start)
VALUES ('global_magic_link', 0, NOW())
ON CONFLICT (id) DO NOTHING;
