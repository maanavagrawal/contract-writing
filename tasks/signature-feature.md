# Signature Feature — Design + Implementation Plan

**Goal**: Agent signs once. Every signature field on every generated PDF (CAR
BRBC, Multi-Board, lease forms) is auto-stamped with that signature. No
re-signing per document. Optional per-document override for buyer/seller
e-signatures stays out of scope for v1.

This is the DocuSign-style "save my signature, apply on demand" pattern,
specialized for our flow where the agent (not counterparties) signs and we're
generating filled PDFs the agent then sends out for client wet-sign.

---

## Why this is the right shape

### Scope: agent signature only, v1

Our user is the listing/buyer's agent. The signature fields on a CAR BRBC fall
into three buckets:

  1. **Agent signature** — "By (Broker/Agent)" rows on page 7. These are the
     agent's own signature, identical across every document, signed at the
     moment of generation.
  2. **Buyer/seller/tenant signature** — counterparty rows. Signed later, on
     wet paper or e-sign. Stays blank in our output today; that's correct.
  3. **Initials boxes** — small "Buyer's Initials" / "Agent's Initials" boxes
     at the bottom of every page. Agent's initials follow the same "sign
     once" pattern.

v1 ships buckets 1 and 3 (agent-side only). Counterparty e-sign is a separate
product (handoff to DocuSign / Dropbox Sign / our own e-sign flow later).

### "Sign once" UX

Today the agent settings page already has `agent.brokerage`, `agent.license`,
etc. — defaults persisted in `agent_defaults` keyed by user. Signature joins
that table as `agent.signature` (base64 PNG) and `agent.initials` (base64 PNG,
smaller). One save in settings → every future generate uses them.

First-time prompt: the first time an agent hits "Generate" without a stored
signature, we open the signature modal. They sign once. We save it. We generate.
Subsequent generates skip the prompt unless they choose to update.

### Two capture modes

- **Draw** — HTML canvas. Pointer events (works for trackpad, mouse, finger on
  touch screens). Clear / Undo / Done buttons. Renders to 600×200 PNG with
  transparent background. The drawn stroke is what gets stamped.

- **Type** — text input → rendered in a cursive web font (Caveat or Allura via
  Google Fonts, ~20KB woff2, loaded only when modal opens). Renders to a 600×200
  PNG via the same canvas (drawText on offscreen canvas). User can pick from 2-3
  cursive styles. The typed name + chosen font become the signature.

Both modes produce the same artifact — a transparent-background PNG at a fixed
aspect ratio (3:1) — so the downstream stamp pipeline is mode-agnostic.

Defaults: tab opens on **Type** because draw-on-trackpad is fiddly and the
typed cursive looks legitimate enough for 90% of use. Agent can switch to draw
and the modal remembers their last choice next session.

### Initials

Same modal, second step: "Initials" prompt after signature. Auto-pre-fills with
first letters of typed name (e.g., "John Smith" → "JS") in the chosen cursive
font. Agent can override or draw their own. Stored separately as
`agent.initials`. We don't try to derive initials from the signature PNG
mechanically — that's a vision problem, the agent just types/draws them.

---

## Architecture

### Storage

`agent_defaults` table already exists with `value TEXT`. Base64-encode the
PNG and store it. A 600×200px transparent PNG is ~8-15KB; base64 inflates ~33%
to ~10-20KB. Fits comfortably in Postgres TEXT, no schema migration needed.

Two new allowed paths:
  - `agent.signature` — full signature PNG, base64-encoded `image/png`
  - `agent.initials`  — initials PNG, same format

Both go in `AGENT_DEFAULTS_ALLOWLIST` in `backend/agent_defaults.py`.

We do NOT store these in the `agent.*` payload sent to the AI mapper — they
never go through GPT. The AI sees them as canonical paths; the fill pipeline
intercepts before any base64 hits the prompt.

### Field detection: who needs a signature?

Two paths today end up at "this field gets a signature stamp":

  1. **/Sig fields** — the AcroForm widget type. CAR BRBC has explicit /Sig
     fields on page 7. These are the easy case.

  2. **/Tx fields the AI tagged as `agent.signature`** — the BRBC's "By
     (Broker/Agent)" row is a /Tx field, not /Sig (because CAR's PDF flattens
     signature placeholders as text widgets). The mapper has to learn that a
     /Tx field whose neighbor text contains "Signature" / "Broker/Agent" / "By
     " on a signature row maps to `agent.signature`, not to `agent.name`.

The mapper update lives in `backend/templates.py` — extend the canonical
schema hint with `agent.signature` and `agent.initials` as legitimate
canonical paths, and add disambiguator language: "By (Broker/Agent)" → signature,
"Agent" (alone) → name.

### Stamping: how the PNG gets into the PDF

`backend/pdf_fill.py` currently writes /V strings to /Tx fields. For signature
fields we need to drop a transparent PNG onto the page at the field's /Rect
coordinates.

Approach: when a field's resolved value is the literal string `__sig:agent__`
or `__sig:initials__` (sentinels emitted by `generate.py` when it sees a
signature canonical path), `pdf_fill` decodes the stored PNG, scales it to fit
the widget rect (preserving aspect, vertical-center, left-align — matches
DocuSign behavior), and stamps it via a pypdf overlay onto the page. The
underlying /Tx field gets cleared (no junk text rendered behind the PNG).

For /Sig fields: same stamp approach. We do NOT generate a cryptographic
digital signature (PKCS#7) — that's a different product. We render the visual
signature only. The /SigFlags handling already in pdf_fill.py keeps the
viewer from complaining.

Implementation detail: pypdf doesn't have a great native overlay-image API, but
the standard pattern is (a) embed the PNG as an XObject in the page resources,
(b) append a `q ... cm ... Do Q` snippet to the page's content stream
positioning the XObject inside the rect. This is well-known territory — a 30-50
line helper.

### Render performance

Stamp work is per-field, per-document. For a CAR BRBC with ~20 signature/initial
fields, that's 20 PNG decodes + 20 XObject embeds. PNG decode is ~1-2ms each
via Pillow; XObject embed is microseconds. Total overhead per generated PDF:
<50ms. Negligible compared to AI mapping (60-180s).

The signature PNG is cached as decoded bytes once per generate call (not per
field) — decoded in `fill_pdf()` setup, reused across all stamps.

### Security

- The PNG sits in `agent_defaults` like any other agent default — same access
  control (`user_id` FK, current_user enforcement).
- We don't expose the signature via any GET endpoint that returns raw bytes;
  the existing `/api/me/defaults` returns it as base64 inside JSON.
- The agent's drawn signature is biometric-adjacent — store-at-rest is the
  Postgres default (Railway provides encryption at rest). No additional crypto
  layer in v1; revisit if SOC2 / state law (CCPA biometric definitions) bites.
- A signature PNG is NOT a legally-binding digital signature. We're producing
  a visual artifact. Document that clearly in the modal: "This adds your
  signature image to forms. For legally-binding e-signature, use DocuSign."

### Backwards compatibility

Agents without a saved signature get the old behavior: signature fields stay
blank, downstream wet-sign workflow unchanged. The first-generate prompt is
the only nudge; "skip for now" leaves them blank without breaking.

Existing /Sig field handling (the `_any_signature_is_signed` walker, the
/SigFlags clear) needs minor adjustment: when we stamp a /Sig field with a
visual, we should NOT mark the /SigFlags bit (we're stamping a visual, not a
crypto signature). Test: pre-sign-flagged templates still open in Preview/Chrome
after generate.

---

## User flow

### First time (no signature on file)

1. Agent uploads CAR BRBC, fills out notes, hits Generate.
2. Backend detects: the mapping has `agent.signature` fields, but
   `agent_defaults.agent.signature` is unset for this user.
3. Generate response includes a `needs_signature: true` flag.
4. Frontend pauses the generate state, opens the signature modal:
   - Default tab: Type (their saved name from defaults pre-fills the input)
   - Tab: Draw
   - Subtle copy: "Your signature will be saved and used on all future
     documents. Change anytime in Settings."
5. Agent picks a font / draws, then prompted for initials (auto-derived from
   name, editable).
6. Save → POST `/api/me/defaults/agent.signature` + `agent.initials` →
   re-fire the generate request → returns signed PDFs.

### Subsequent generates

No modal. Signature pulled from defaults, stamped, done.

### Updating signature

Settings → Defaults section → "Signature" row shows a thumbnail of the
current PNG + an "Edit" button → opens the same modal pre-loaded.

### Per-document signing flow (deferred to v2)

If we later need per-counterparty signing, we add a separate "Send for signature"
button on a generated doc. v1 punts and assumes wet-sign or external e-sign
for counterparties.

---

## What gets stamped where

Concretely on a CAR BRBC (the goal-state form):

| Field         | Page | Canonical path              | Stamp source        |
|---------------|------|-----------------------------|---------------------|
| f_006_001     | 7    | agent.signature             | agent.signature PNG |
| f_006_002     | 7    | agent.name (printed)        | text (existing)     |
| f_006_004     | 7    | agent.signature (co-agent)  | agent.signature PNG |
| Initials @ bottom of each page | 1-12 | agent.initials | agent.initials PNG |

The mapper today often sends "By (Broker/Agent)" → `agent.name`. That's wrong
once signatures exist. The canonical schema hint update teaches it: "By
(Broker/Agent)" rows go to `agent.signature`; "Agent" (printed) rows stay
`agent.name`.

---

## Post-review revisions (locked decisions)

### Design (from /plan-design-review)

- **No mid-generate ambush.** Detect signature fields at upload/parse time;
  show a non-blocking banner on the doc card: "This form needs your signature.
  Set it up now →". The modal-on-generate path is the fallback if they ignore
  the banner.
- **Type tab pre-renders their name.** Modal opens with `agent.name` already
  rendered in the default cursive font. First impression: "that's my
  signature" not "pick a font."
- **Initials merge into the same modal as a second pane**, not a separate
  step. Pre-rendered from first-letter-of-each-word, one-tap to override.
- **Settings discoverability**: signature thumbnail appears on the Generate
  confirmation screen ("Signing as: [thumbnail] — change"). This is where
  the "did I save the right one?" anxiety lives.
- **Legal copy rewrite**: "Your signature is stamped on every form you
  generate. Clients still sign separately (wet ink, DocuSign, etc.)." No
  L-word, no churn-bait disclaimer.
- **Allow ~10% horizontal bleed** past the rect, clipped to page bounds.
  Strict containment reads as stamped-by-bureaucrat; slight bleed reads
  human.
- **Migration moment for the 2 existing paying agents**: text/email them
  before the field-detection goes live. Don't let them discover the feature
  through an interrupted workflow.

### Engineering (from /plan-eng-review)

- **Image stamping = reportlab overlay + `pypdf.PageObject.merge_page()`**,
  NOT hand-rolled XObject embedding. reportlab handles RGBA alpha correctly
  (the /SMask trap is real) and merge_page handles /Rotate cleanly. Adds
  one small dep — acceptable.
- **/Sig widget handling = replace, not overlay.** Strip the /Sig widget
  from `/AcroForm /Fields`, add a read-only /Tx-equivalent widget whose
  appearance stream is the signature XObject. Removes "Sign Here" caret
  + any future /SigFlags ambiguity. Cleanest semantics.
- **Sentinel = dataclass, not string.** Change `pdf_fill` mapping type from
  `dict[str, str]` to `dict[str, str | SigStamp]`. `SigStamp` is a frozen
  dataclass with `kind: Literal["agent", "initials"]` and `png_bytes:
  bytes`. Same branching pattern pdf_fill already uses for /Btn vs /Tx.
- **Mapper retraining = additive only.** Keep `_HANDFILL_NAME_TOKENS`
  intact. Add a positive routing rule to `_EXTRA_TO_CANONICAL_RULES`:
  `(("signature",), ("agent", "broker", "by")) → agent.signature`. The
  AI's existing "this is handfill" suppression still fires on bare
  "signature" extras; the new rule promotes only the agent/broker subset.
  Protects 93% MIN.
- **/Rotate is non-negotiable**: synthetic /Rotate=90 fixture +
  counter-rotation matrix in the stamping primitive. This is the kind of
  bug that ships fine for 3 months and then a CAR template comes through
  rotated and the signature appears sideways.
- **Storage**: 200KB API-layer cap on `agent.signature` / `agent.initials`
  uploads. Postgres TEXT/base64 is fine at our scale.
- **Failure mode**: corrupt PNG → typed exception caught in `generate.py`,
  fields stay blank, response includes
  `warnings: ["signature_image_corrupt"]`. Never 500.
- **Generate response shape** (replaces hand-wavy `needs_signature: true`):
  ```json
  {
    "documents": [...],
    "signature_status": {
      "required_by_template": true,
      "user_has_signature": false,
      "fields_left_blank": 4
    }
  }
  ```
  Frontend opens modal only when `required_by_template &&
  !user_has_signature`. The four-cell shape covers all combinations
  including saved+template-has-no-sig-fields (no modal, clean run) and
  saved+stamp-failed (soft warning).
- **Test fixtures (minimum)**:
  - Unit: `_stamp_image_in_rect` on synthetic 1-page /Tx PDF
  - Unit: same on /Rotate=90 page — assert upright
  - Unit: corrupt PNG → typed exception
  - Unit: /Sig widget replacement → /SigFlags bit 1 cleared, widget gone
  - Integration: existing CAR BRBC mapping eval — assert 93% MIN holds AND
    signature-row fields now route to `agent.signature`
  - E2E (gated): full BRBC upload + signature default + generate → count
    XObjects on output matches expected stamp count (~20). No visual diff
    (flaky).

---

## Open questions deferred to v2

1. **Cursive font choice** — Caveat (handwriting style, free, Google Fonts) vs
   Great Vibes (more "formal cursive"). Lean Caveat: looks more like a
   real signature, less like a script-font joke. Possibly offer both as styles.

2. **PNG vs SVG storage** — SVG would scale crisper for the drawn signature.
   But typed-cursive renders to PNG anyway, and the visual rect we stamp is
   small enough that 600×200 PNG resamples fine. Going PNG for uniformity.

3. **What happens if the AI doesn't tag a signature field as
   agent.signature?** — Existing handfill detection currently catches these
   (suppresses them from the low-confidence banner). After this change, those
   handfill suppressions need to flip — signature fields go from "handfill, not
   our problem" to "auto-stamp from agent.signature." The
   `_HANDFILL_NAME_TOKENS` list shrinks; the mapper does more work.

4. **Initials on every page** — CAR BRBC has 12 initials boxes (one per page).
   That's a lot of stamping; we want to confirm the PNG stamping path doesn't
   produce visible artifacts when the rect is small (~30×15 pt). Test fixture
   needed.

---

## Implementation order

1. **Backend storage**: extend `AGENT_DEFAULTS_ALLOWLIST` with `agent.signature`
   and `agent.initials`. Smoke-test PUT/GET round-trip with a 15KB base64 PNG.

2. **Backend stamping primitive**: `pdf_fill._stamp_image_in_rect(page, rect,
   png_bytes)` helper. Unit-test it on a single synthetic PDF.

3. **Mapper update**: add `agent.signature` / `agent.initials` to the canonical
   schema hint in `templates.py`, write disambiguator examples, retire the
   `signature` token from `_HANDFILL_NAME_TOKENS` (it now maps to a canonical).
   Re-run eval to confirm no regression on CAR BRBC accuracy.

4. **Generate wiring**: `generate.py` resolves `agent.signature` →
   `__sig:agent__` sentinel → `pdf_fill` recognizes the sentinel and stamps
   the PNG instead of writing text.

5. **Frontend modal**: build the capture UI as `frontend/modules/signature.js`.
   Two tabs, canvas + font preview, save endpoint hookup. Lazy-load Google
   Fonts.

6. **First-time prompt**: generate-response `needs_signature: true` → modal
   pops, then retries generate.

7. **E2E**: upload CAR BRBC fresh, set agent defaults including signature,
   generate. Render the output PDF. Visually verify the signature appears on
   every signature field. Initials on every page. No regressions on existing
   filled-text fields.

---

## Acceptance criteria for /goal

The goal is: "writing a signature once populates all the fields correctly and
smoothly."

Concretely:

- Agent visits settings, opens signature modal, types name OR draws on
  trackpad. Sees a live preview.
- Hits save. The signature persists to `agent_defaults`.
- Uploads a CAR BRBC and generates a filled version.
- The generated PDF, opened in Chrome/Preview, shows the agent's signature
  rendered on every "By (Broker/Agent)" row and every "Agent's Initials" box,
  positioned within the field rect, visibly correct.
- No re-prompt for signature on subsequent generates.
- Existing fields (name, license, brokerage) still populate as before — no
  collateral damage.
