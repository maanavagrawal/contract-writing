# memoir

Real estate paperwork, automated. FastAPI + Postgres + a static frontend.
Magic-link auth via Resend. Multi-tenant — each user uploads their own PDF
templates and the AI maps fields to a canonical transaction schema.

## Local dev

Requires Python 3.11+ and Docker (for the local Postgres).

```bash
# 1. Create a venv and install deps
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Start a local Postgres
docker run -d --name memoir-pg -e POSTGRES_PASSWORD=postgres -p 5544:5432 postgres:16-alpine

# 3. Set env vars (or put them in .env, which is gitignored)
export DATABASE_URL=postgresql://postgres:postgres@localhost:5544/postgres
export OPENAI_API_KEY=sk-...
# RESEND_API_KEY is optional in dev — without it, magic-link emails are
# logged to stdout instead of sent.

# 4. Run migrations + start the server
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000). Sign in with any email;
the magic link will appear in the server log if RESEND_API_KEY isn't set.

## Tests

```bash
.venv/bin/pytest
```

Tests boot a session-scoped Postgres container via testcontainers. Docker
must be running. Set `DOCKER_HOST` if your Docker socket isn't at the
default path (the test conftest auto-detects via `docker context inspect`).

## Production deploy (Railway)

memoir runs as a single FastAPI service that also serves the static frontend.
No Vercel split — same-origin auth keeps the cookie story simple.

1. **Push the repo to GitHub.** Connect Railway to it.
2. **Add a Postgres add-on** in the Railway dashboard. Railway sets
   `DATABASE_URL` automatically.
3. **Add a Volume** mounted at `/app/templates/pdf` (1GB is plenty for a few
   users uploading 5-10 PDFs each).
4. **Add a Volume** mounted at `/app/backend/mappings` (mapping JSONs
   generated at upload time).
5. **Set env vars** in Railway:
   - `OPENAI_API_KEY` — your OpenAI key
   - `RESEND_API_KEY` — your Resend key (sender domain must be verified)
   - `EMAIL_FROM` — e.g. `memoir <noreply@yourdomain.com>` (must match Resend domain)
   - `APP_BASE_URL` — your public URL, e.g. `https://memoir.up.railway.app`
6. **Resend setup** — at [resend.com](https://resend.com), verify your sender
   domain (add SPF + DKIM + DMARC records to DNS). Without verification,
   magic-link emails will land in spam or be rejected.
7. **Deploy.** Migrations run automatically on startup.

### Health check

```
GET /api/health
```

Returns `{"ok": true}` without auth. Use as Railway's health-check endpoint.

### What's NOT in scope for this deploy

- Stripe payment integration (separate task)
- File storage on R2/S3 (Railway Volume is enough for low-volume users)
- Per-user rate limiting on /api/extract (defer until 5+ users)
- Audit log / admin view (defer until you need debugging at scale)

## Architecture

```
Browser (cookie auth)
    │
    ▼
Railway: FastAPI container
    ├─→ Postgres (Railway add-on)
    │     · users, sessions, templates, transactions
    ├─→ Volume: /app/templates/pdf (uploaded PDFs)
    ├─→ Volume: /app/backend/mappings (mapping JSONs)
    ├─→ OpenAI API (gpt-5 for extract + map)
    └─→ Resend API (magic-link emails)
```

- Generated PDFs are NOT persisted — they round-trip as base64 in the API
  response. This is the privacy decision; we never store filled documents.
- Sessions are 14 days. Magic-link tokens are 15 minutes, single-use.
- Token plaintext never touches the DB — only SHA-256 hashes are stored.

See `backend/auth.py` for the full magic-link state machine and
`backend/migrations/` for the schema.
