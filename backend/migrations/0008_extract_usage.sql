-- Per-user, per-minute counter for the live-tier /api/extract/stream
-- endpoint. The frontend already throttles, but the server needs its own
-- gate so a buggy or malicious client can't burn dollars on gpt-5-mini.
--
-- One row per (user_id, minute) bucket; ON CONFLICT increments count. The
-- table is also useful telemetry: "which users hit the cap?" answers from
-- a single SELECT.

CREATE TABLE IF NOT EXISTS extract_usage (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    minute      TIMESTAMPTZ NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, minute)
);

CREATE INDEX IF NOT EXISTS idx_extract_usage_user_minute
    ON extract_usage(user_id, minute);
