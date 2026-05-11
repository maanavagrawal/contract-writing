-- Add SHA-256 hash of the uploaded PDF to templates.
--
-- Cache key for the AI mapping: when a user re-uploads the same PDF bytes
-- (same form version, fresh upload), we can skip the 60-90s AI call and
-- copy the existing mapping. When a user uploads a different revision
-- (Multi-Board v8.0 -> v8.1, or a watermarked variant), the hash differs
-- and we run fresh mapping.
--
-- Nullable for back-compat with rows inserted before this column existed.
-- Visual-enrichment + cache-lookup code reads it via WHERE pdf_sha256 = %s.

ALTER TABLE templates
    ADD COLUMN IF NOT EXISTS pdf_sha256 TEXT;

CREATE INDEX IF NOT EXISTS idx_templates_pdf_sha256
    ON templates(pdf_sha256);
