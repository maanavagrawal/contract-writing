-- Whisper API daily quota tracking + idempotency.
--
-- whisper_usage tracks per-agent per-day audio seconds + request count. Used
-- to enforce a daily voice cap (default 600s = 10 minutes) so a runaway
-- client can't burn the bill. (user_id, day) is the natural PK because we
-- aggregate per day.
--
-- whisper_idempotency stores recent request_ids so a network-retry within
-- 60s gets the cached transcript instead of double-billing. Rows expire on
-- read via a TTL check (we don't need a background job — opportunistic
-- cleanup on each insert keeps the table tiny for our scale).

CREATE TABLE IF NOT EXISTS whisper_usage (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    seconds     INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE INDEX IF NOT EXISTS idx_whisper_usage_user_day
    ON whisper_usage(user_id, day);

CREATE TABLE IF NOT EXISTS whisper_idempotency (
    request_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    transcript  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whisper_idempotency_user
    ON whisper_idempotency(user_id, created_at);
