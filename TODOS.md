# TODOS

Captured during plan reviews. Pull from here when scoping future iterations.

---

## Coordinate-overlay templating for non-AcroForm PDFs

**What:** When a user uploads a non-AcroForm PDF (scanned, image-only, flattened),
allow drawing rectangles in the review UI to define field positions, then fill via
coordinate-based text rendering.

**Why:** A real subset of real estate forms are scanned or flattened. Without this,
those templates are rejected at upload time. Eventually a hard gap.

**Pros:** Unblocks more templates; "works for any PDF" is the long-term promise.

**Cons:** ~3-5 days of work. Coordinates break when the source PDF is revised.
Adds a second fill engine to maintain.

**Context:** V1 deliberately rejects non-AcroForm PDFs at upload. Add when users
hit the limitation in practice.

**Depends on:** Pillar 2 shipped (upload UI + review infrastructure must exist).

**Captured:** 2026-05-05 via /plan-eng-review.

---

## Extract DESIGN.md from existing CSS

**What:** Document the existing design system in DESIGN.md: color tokens, type
scale, spacing, component vocabulary (buttons, pills, cards, drawers), and the
rules (no emojis, one accent color, status pill semantics, dark-only theme).

**Why:** Speeds up future contributions, prevents design drift. Both
`/plan-design-review` and `/design-review` calibrate against DESIGN.md when it
exists — reviews get sharper.

**Pros:** ~30 min with CC. Documents the intentional choices already in code.

**Cons:** Maintenance burden if CSS evolves substantially.

**Context:** [frontend/styles.css](frontend/styles.css) is well-tokenized but
undocumented. Plan-design-review reviewer (Claude) read the CSS directly to
calibrate. Future reviewers won't have that depth of read.

**Depends on:** Pillar 2 shipped (avoid documenting a moving target).

**Captured:** 2026-05-05 via /plan-design-review.

---

## Generate visual mockups via gstack designer (after OpenAI org verification)

**What:** Once the user verifies their OpenAI org at
[platform.openai.com/settings/organization](https://platform.openai.com/settings/organization),
re-run `/plan-design-review` mockup generation OR run `/design-shotgun` to produce
visual mockups for the 3 new surfaces (template library, mapping review, upload modal).

**Why:** ASCII wireframes are a strong stand-in but real mockups catch AI-slop in
actual rendering and make design intent visceral. Worth doing before Pillar 2
implementation kicks off so the implementer has a precise visual target.

**Pros:** Better visual reference, catches edge cases the wireframes miss.

**Cons:** Requires one-time OpenAI org verification.

**Context:** Mockup generation failed during /plan-design-review on 2026-05-05 with
"OpenAI organization verification required." ASCII wireframes embedded in
[tasks/todo.md](tasks/todo.md) stand in for now.

**Depends on:** OpenAI org verified.

**Captured:** 2026-05-05 via /plan-design-review.

---

## Reuse PDF.js + canvas overlay component beyond mapping review

**What:** Structure the Pillar 2 mapping review UI so the PDF.js + rectangle overlay
component can be reused for: (a) DocuSign tab placement when Pillar 3 lands live,
(b) a "preview filled PDF" panel before generate, (c) field-rect debugging tools.

**Why:** PDF.js + overlay is a full day of frontend work. Reusing amortizes that cost.

**Pros:** Cleaner downstream code, faster Pillar 3 build.

**Cons:** Slightly more upfront design pressure during Pillar 2 (avoid entangling
mapping state with rendering state).

**Context:** Plan treats this as one-off review UI. When implementing Pillar 2,
extract a `<PdfFieldOverlay>` component that takes (pdfUrl, rects, onRectClick)
and is mapping-agnostic. The mapping page composes it with its own state.

**Depends on:** Pillar 2 in flight.

**Captured:** 2026-05-05 via /plan-eng-review.

---

## DRY up CANONICAL_SCHEMA_HINT and _build_canonical_path_allowlist

**What:** Generate the prompt hint text in [backend/templates.py:159-209](backend/templates.py#L159-L209) from the same Pydantic introspection that builds [_build_canonical_path_allowlist](backend/templates.py#L331-L380). Today the hint is hand-maintained as a 50-line text block while the allowlist is derived from `TransactionFields.model_fields`.

**Why:** Drift risk. Adding a new canonical field to `TransactionFields` requires also editing the hint text or the AI proposes paths that get rejected. Silent failure mode.

**Pros:** Eliminates a class of subtle bugs. ~30 min with CC.

**Cons:** Hint text is more than just field names — it has prose explanations and rules. A pure introspection-driven hint would lose that. Likely pattern: introspect names + types, append hand-written prose for the rules.

**Context:** Pre-existing code smell, not a regression. Not a deploy blocker.

**Depends on:** Nothing.

**Captured:** 2026-05-08 via /plan-eng-review (production deploy plan).

---

## Per-user rate limiting on /api/extract and /api/templates/upload

**What:** Add `slowapi` with per-user-id key. Suggested limits: 5 extracts/minute, 3 template uploads/hour. Apply only to OpenAI-calling endpoints.

**Why:** Burst protection on OpenAI budget. A buggy frontend or curious user shouldn't be able to burn $50 of API credits in a minute.

**Pros:** Bounded blast radius. ~30 min to add.

**Cons:** Premature at 2 friendly users. Adds a dep + middleware.

**Context:** Defer until first user complaint about cost OR user #5+. Add log line on every OpenAI call (cost/tokens) before adding the limiter so we know what "normal" looks like.

**Depends on:** Auth shipped (need user_id as the rate-limit key).

**Captured:** 2026-05-08 via /plan-eng-review.

---

## Redact magic-link plaintext from dev-mode logs

**What:** In [backend/email_send.py:43-47](backend/email_send.py#L43-L47), the dev-mode fallback logs the full email body (including the magic-link URL with plaintext token) when `RESEND_API_KEY` is unset. If a production deploy ever boots without that env var (typo, accidental unset, post-rotation gap), Railway logs end up containing valid magic-link URLs that anyone with dashboard access can use to log in as that user during the 15-minute TTL.

**Why:** Defense in depth. The Railway dashboard is admin-only, but ops staff, contractors, or anyone with read-only Railway access could see and use those links. The current logs already truncate to 200 chars (`html[:200]`) but the link is in the first 200 chars.

**Pros:** ~5 minutes to redact. Eliminates a real (if narrow) audit-log-as-credential-store risk.

**Cons:** Makes dev-mode debugging harder — you'd need to log into the database to find the pending token hash.

**Context:** Better fix: refuse to start the server in production if `RESEND_API_KEY` is unset. The dev-mode warning path should never run in prod. Add a `MEMOIR_ENV=production` env var that turns the fallback into a hard error.

**Depends on:** Nothing.

**Captured:** 2026-05-10 via /review.

---

## Audit log + admin view

**What:** New `events` table tracking: auth login/logout, magic-link sent/redeemed, template upload/delete, extract/generate calls (with duration + token count). Plus a minimal `/admin` view (gated to a hardcoded admin email) listing recent events per user.

**Why:** Debugging at scale. "Whose template was that?" / "Why did this user's extract fail?" / "Are they actually using it?". Today Railway logs cover this; at 5+ users you'll want structured queryable history.

**Pros:** Enables observability without external tools. ~2 hours.

**Cons:** Adds a write per request. Schema decisions to make. Privacy implications — log events but never log user-data content.

**Context:** Trigger at user #5+ OR first time you can't answer a debugging question from Railway logs alone.

**Depends on:** Postgres + auth shipped.

**Captured:** 2026-05-08 via /plan-eng-review.
