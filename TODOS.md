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
