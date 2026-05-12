-- Promote whisper_idempotency PK from request_id to (user_id, request_id).
--
-- Why: a global PK on request_id meant a different user grabbing the same
-- request_id within the 60s window would silently no-op on INSERT (ON
-- CONFLICT (request_id) DO NOTHING), so their own retry never hit the
-- cache and re-billed every time. Cache reads were already user-scoped, so
-- this isn't a privacy bug — it's a "your own retries leak money" bug.
-- Composite PK fixes both the INSERT path and matches the cache-read shape.

ALTER TABLE whisper_idempotency DROP CONSTRAINT IF EXISTS whisper_idempotency_pkey;
ALTER TABLE whisper_idempotency ADD PRIMARY KEY (user_id, request_id);
