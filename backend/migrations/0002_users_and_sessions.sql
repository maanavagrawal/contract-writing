-- Magic-link auth + session storage.
--
-- users: identity is just an email. pending_token_hash holds the SHA-256 of
--   the most recent unredeemed magic-link token (we never store the plaintext
--   so a DB read can't impersonate). pending_token_expires gates redemption.
--   token_request_count + token_request_window_start implement per-user
--   rate-limiting (3 magic links / hour) without a separate table.
--
-- sessions: one row per logged-in browser. token_hash is SHA-256 of the
--   session-cookie value; like the magic-link, plaintext lives only in the
--   cookie. expires_at is the hard cap; logout deletes the row.
--
-- Both tables index on the lookup hash. We also index users(email) lower-cased
-- so case-insensitive lookup (alice@x vs ALICE@x) is one query, no scan.

CREATE TABLE IF NOT EXISTS users (
    id                          TEXT PRIMARY KEY,
    email                       TEXT NOT NULL,
    pending_token_hash          TEXT,
    pending_token_expires       TIMESTAMPTZ,
    token_request_count         INTEGER NOT NULL DEFAULT 0,
    token_request_window_start  TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower
    ON users (LOWER(email));

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions(user_id);
