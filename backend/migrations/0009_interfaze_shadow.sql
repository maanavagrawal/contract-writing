-- Interfaze shadow log.
--
-- Records what Interfaze "saw" on every flattened-PDF upload that triggered
-- the field_synth path. Never affects user-facing output — purely an audit
-- log we can grep through to decide whether to promote Interfaze to the
-- primary detection path.
--
-- One row per template_id. We store the raw counts and a small JSON sample
-- of detected fields (not the full list — the AI mapping output captures
-- the canonical-path side). When/if we're ready to ship Interfaze as
-- primary, this table is the dataset that proves it's worth it.

CREATE TABLE IF NOT EXISTS interfaze_shadow_log (
    id              SERIAL PRIMARY KEY,
    template_id     TEXT NOT NULL,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cv_field_count  INTEGER NOT NULL,
    interfaze_field_count INTEGER NOT NULL,
    interfaze_latency_ms INTEGER NOT NULL,
    interfaze_cost_usd_estimate NUMERIC(10, 6) NOT NULL DEFAULT 0,
    -- Diff metadata for quick triage:
    --   fields_unique_to_cv: rects CV found that Interfaze missed
    --   fields_unique_to_interfaze: rects Interfaze found that CV missed
    fields_unique_to_cv INTEGER NOT NULL DEFAULT 0,
    fields_unique_to_interfaze INTEGER NOT NULL DEFAULT 0,
    -- Sample payloads (capped at ~10 entries each, JSON) for spot-checking.
    cv_sample_json      TEXT,
    interfaze_sample_json TEXT,
    interfaze_error     TEXT,          -- non-null if the shadow call failed
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_interfaze_shadow_user
    ON interfaze_shadow_log(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_interfaze_shadow_template
    ON interfaze_shadow_log(template_id);
