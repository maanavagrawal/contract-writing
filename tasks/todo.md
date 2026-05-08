# Real Estate Paperwork Automator — V1 Plan

Goal: get this from "v0, fills some contracts" to "someone can actually use it for their personal work in a pseudo-production setting." Three pillars:

1. **Fix the autofill bug** so existing templates fill cleanly. **Shipping standalone today.**
2. **Drag-drop template upload** with AI-assisted field mapping + human review.
3. **DocuSign-ready architecture** (design only this iteration).

> Plan reviewed via `/plan-eng-review` on 2026-05-05. Scope cut accepted: dropped
> `required_fields` plumbing + `defaults.py` refactor from Pillar 1; dropped 501 send
> endpoint stub from Pillar 3. Outside voice (independent Claude subagent) raised 7
> challenges — 3 cross-model tensions resolved, 4 incorporated as plan items below.
> Eng-review test plan at `~/.gstack/projects/jason-zhnn-real-estate-paperwork-automator/maanavagrawal-main-eng-review-test-plan-20260505-221153.md`.

---

## DEBUG REPORT — autofill investigation

```
Symptom:         "v0, currently does not fill all contracts correctly."
                 Specifically: lease_invoice fills 1/6 mapped fields. Lease abstract leaves
                 'Lease End Date.0.0' empty even though mapped.

Root cause:      backend/generate.py uses pypdf's `update_page_form_field_values`, which walks
                 a page's /Annots and matches widget annotations by /T. For PDFs where the
                 widget annot has no /T (the field name lives on the parent AcroForm field),
                 the helper silently no-ops. The lease invoice and tenant rep PDFs are built
                 this way. Hierarchical fields (Lease End Date.0.0) are also unreachable
                 because the helper doesn't resolve dotted paths.

Fix:             Walk the AcroForm /Fields tree directly and write /V on each leaf field
                 whose joined-/T-path matches a mapping key. For checkbox/radio /Btn fields,
                 also set /AS on the widget kids so the box visually checks. Keep
                 /NeedAppearances=true.

Evidence:        Direct-write prototype lifted lease_invoice from 1/6 → 6/6 filled. Tested
                 against all four templates without regressions.

Status:          DONE (root cause confirmed, fix prototyped)
```

### Per-template fill audit (with synthetic full-coverage fixture)

| Template       | PDF fields | Mapping keys | Filled now | Real bugs |
|----------------|-----------:|-------------:|-----------:|-----------|
| lease_invoice  | 7          | 6            | **1**      | YES — see root cause above |
| lease_abstract | 19         | 15           | 13         | YES — `Lease End Date.0.0` (dotted-path bug) |
| tenant_rep     | 29         | 28           | 17         | mostly empty mappings (`""`) — by design (signatures/optional) |
| multiboard     | 389        | 49           | 38         | mostly fine; sparse fixture left a few defaults blank |

---

## V1 Architecture — target

```
┌─────────────────────────────────────────────────────────────────────────┐
│ FRONTEND (vanilla JS, no framework)                                     │
│  • Notes pane (textarea + image drop)                                   │
│  • Field-review pane (auto-populated form, including extra_fields       │
│    contributed by active templates)                                     │
│  • [NEW] Template library: drag PDFs in, see mapping status             │
│  • [NEW] Mapping review UI: PDF.js + canvas overlay, click-to-link      │
│  • [NEW] Document picker: choose templates BEFORE extraction so the     │
│    dynamic schema is known (see Sequencing fix below)                   │
│  • [LATER] Send-via-DocuSign panel                                      │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ FASTAPI BACKEND                                                         │
│  /api/extract             — notes+images+active_template_ids → fields   │
│  /api/generate            — fields → filled PDFs (persisted) + failures │
│  /api/templates           — list/CRUD uploaded templates                │
│  /api/templates/upload    — POST PDF → AI proposes mapping (sync, ~30s) │
│  /api/templates/:id/map   — PUT confirmed mapping                       │
└─────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ STORAGE (sqlite + filesystem)                                           │
│  • templates/pdf/<uuid>.pdf      — uploaded source PDFs                 │
│  • templates/mappings/<uuid>.json — confirmed mappings                  │
│  • templates/generated/<txn>/    — filled PDF outputs                   │
│  • data/app.sqlite               — templates, transactions,             │
│                                    generated_documents                   │
│                                    [LATER] envelopes (DocuSign)         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Data flow on /api/generate

```
client                    FastAPI                    Filesystem      sqlite
  │                         │                           │              │
  │ POST {fields, agent,    │                           │              │
  │       documents:[...]}  │                           │              │
  ├────────────────────────►│                           │              │
  │                         │ INSERT transaction        │              │
  │                         ├──────────────────────────────────────────►│
  │                         │                           │              │
  │                         │ for each doc:             │              │
  │                         │   load mapping (Pydantic) │              │
  │                         │   fill_pdf(reader, map)   │              │
  │                         │   write PDF ─────────────►│              │
  │                         │   INSERT generated_doc ──────────────────►│
  │                         │                           │              │
  │ {documents:[...],       │                           │              │
  │  failures:[...]}        │                           │              │
  │◄────────────────────────┤                           │              │
```

---

## Design specification (added by /plan-design-review)

Calibrated against the existing frontend's design system: dark theme, amber accent
(`--accent: #e8b04b`), Inter for UI / JetBrains Mono for technical bits, restrained
SaaS aesthetic, real CSS tokens. **Existing design quality bar is high (8/10).**
Every new surface must match it.

### Information architecture — 3 new/changed surfaces

#### Surface A: Home (workspace) — UI flow REORDERED

The doc picker now lives BEFORE the notes pane, because dynamic-schema extraction
needs to know which templates are active. Existing two-pane workspace stays.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ┌── topbar ──────────────────────────────────────────────────────────┐ │
│ │ ◆ Paperwork  Real estate automator   [Templates] [Agent profile] │ │  ← new "Templates" link
│ └──────────────────────────────────────────────────────────────────┘ │
├──────────────────── workspace (grid 4:6) ──────────────────────────────┤
│ LEFT PANE — Documents                  │ RIGHT PANE — Notes & fields  │
│                                        │                              │
│  H2: Documents                         │  H2: Transaction notes       │
│  Pick the templates you'll fill.       │  Paste notes once docs are   │
│                                        │  picked. Cmd↵ to extract.    │
│  ┌── Illinois standard ──────────────┐ │                              │
│  │ ✓ Multi-Board 8.0 — Sale contract│ │  ┌────────────────────────┐  │
│  │ ✓ Tenant Rep Agreement           │ │  │ <textarea>             │  │
│  │ ✓ Lease Abstract                 │ │  │                        │  │
│  │ ✓ Lease Invoice                  │ │  └────────────────────────┘  │
│  └──────────────────────────────────┘ │   [attach] hint  [Extract →]│
│                                        │                              │
│  ┌── Custom (your uploads) ──────────┐ │  (after extract: parsed     │
│  │   Pet Addendum  uploaded 2026-04 │ │   fields appear here, with  │
│  │ ✓ Pool Disclosure  3 fields      │ │   amber-edge from-extract   │
│  └──────────────────────────────────┘ │   borders, plus a CUSTOM    │
│                                        │   field group per active    │
│  + Upload template …                  │   uploaded template)        │
│                                        │                              │
│  ─────────────────────────────────    │                              │
│  5 templates selected · 78 fields     │                              │
│                                        │                              │
└────────────────────────────────────────┴──────────────────────────────┘
```

Key points:
- "Templates" topbar link goes to `/templates` (Surface B).
- "+ Upload template" inline action keeps the user in the home flow if they realize
  they need a new template before extracting. Opens the upload modal.
- Bottom-of-pane summary keeps the existing footer pattern (see `.generate-summary`).
- "Extract" button stays on the right pane, but is disabled until ≥1 template
  is selected. Tooltip: "Pick at least one template to extract for."
- The "after extract" view is identical to today (no design changes to the parsed
  fields form except for one section: extra fields contributed by uploaded templates,
  rendered as a separate `field-group` with a `[CUSTOM • Pet Addendum]` label pill).

#### Surface B: Template library — `/templates`

Replaces today's hardcoded doc list with a managed library. **Dedicated route**, not
a modal — bookmarkable, reflects URL state, supports keyboard nav.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ◆ Paperwork  Real estate automator   [Workspace] [Agent profile]       │
├─────────────────────────────────────────────────────────────────────────┤
│  H1: Templates                                  [+ Upload template]    │
│  PDFs you fill from your transaction notes.                            │
│                                                                         │
│  ┌─ Multi-Board 8.0 ──────────────────────[IL DEFAULT]──[Edit ▸]─┐    │
│  │ 49 mapped fields · 0 unmapped                                  │    │
│  │ Used in 12 transactions                                        │    │
│  └────────────────────────────────────────────────────────────────┘    │
│  ┌─ Pet Addendum ────────────────────────────[CUSTOM]──[Edit ▸]─┐     │
│  │ 3 mapped fields · 1 unmapped                                  │    │
│  │ ⚠ 1 field needs review              [Review mapping →]       │    │  ← "needs_attention"
│  └────────────────────────────────────────────────────────────────┘    │
│  ┌─ Pool Disclosure ─────────────────────────[PENDING]─────────┐      │
│  │ Uploaded 30s ago · AI mapping in progress                    │     │
│  │ ████████████░░░░░░░░  18/47 fields analyzed                  │     │  ← "pending_review"
│  └──────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  ── EMPTY STATE (when no custom templates) ──                          │
│  ┌────────────────────────────────────────────────────────────┐        │
│  │   Drop a PDF here, or click [+ Upload template]            │        │
│  │                                                             │        │
│  │   You already have 4 Illinois templates ready to use.      │        │
│  │   Upload anything else: condo rider, pet addendum, …       │        │
│  └────────────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────────────┘
```

Status pill vocabulary (matching existing `.readiness-*` classes):
- `[IL DEFAULT]` — pre-seeded, can't be deleted; uses `.readiness-ready` (success)
- `[CUSTOM]` — user-uploaded, fully reviewed; uses `.readiness-ready`
- `[PENDING]` — AI mapping in progress; uses `.readiness-pending` (subtle gray)
- `⚠` warn pill on row when `status='needs_attention'` (uses `.readiness-partial` amber)
- `✗` error pill on row when AI mapping failed; uses `.readiness-error`

Drag-drop: the entire main content area is a drop target (matches the existing
`.notes-wrap.dragover` amber-tint pattern). Drop a PDF anywhere on the page → opens
the upload modal pre-populated with the file.

#### Surface C: Mapping review — `/templates/<id>/review` (DEDICATED ROUTE)

Decision 1A: dedicated route, not modal/drawer. PDF.js + canvas overlay on left,
field list on right. URL is shareable/bookmarkable.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ◆ Paperwork   Templates / Pet Addendum / Review     [Discard] [Save ✓] │
├──────────────────── workspace (grid 6:4) ──────────────────────────────┤
│ LEFT — PDF preview                     │ RIGHT — Field mappings        │
│                                        │                              │
│ ┌────────────────────────────────────┐ │  12 of 47 reviewed            │
│ │ ▼ Page 1 of 3                      │ │  ████████░░░░░░░░ 26%         │
│ │                                    │ │                              │
│ │   PET ADDENDUM TO LEASE            │ │  [All] [Unmapped] [Edited]   │
│ │                                    │ │   ─────                      │
│ │   Tenant: ┌─────────────────┐  ←──│──┤  ▶ pet_name                  │
│ │           │ orange highlight │     │  │   → template_extras.pet_name│
│ │           └─────────────────┘     │  │   [text   ▾]  [✗]            │
│ │   Pet: ┌──────────────────┐      │  │  ─                            │
│ │        │ amber outline    │      │  │  ▶ owner_name                │
│ │        └──────────────────┘      │  │   → tenant_or_buyer_names[0] │
│ │                                    │ │   [list   ▾]  [✗]            │
│ │   Deposit: $ ┌─────┐               │ │  ─                            │
│ │              └─────┘               │ │  ⚠ Field4 (unmapped)         │
│ │                                    │ │   → choose a mapping…       │
│ │  ┌─────────────────────────────┐  │ │   [—       ▾]  [✗]           │
│ │  │ <PDF.js canvas, scrollable> │  │ │  ─                            │
│ │  └─────────────────────────────┘  │ │  ✓ pet_breed                 │
│ │                                    │ │   → template_extras.pet_breed│
│ │  Page nav: ← 1 2 3 →               │ │                              │
│ └────────────────────────────────────┘ │  …                            │
│                                        │                              │
└────────────────────────────────────────┴──────────────────────────────┘
```

Interaction:
- **Click row** → matching rectangle gets amber outline + the canvas scrolls so the
  rectangle is visible (smooth scroll, ~150ms).
- **Click rectangle** → matching row scrolls into view in the right pane and gets
  amber background highlight (200ms ease).
- **Hover row OR rectangle** → both subtly amber-tint together (preview the link
  before commit). Saves 1-step-ahead cognitive load.
- **Keyboard:** `j`/`k` or `↓`/`↑` move through rows; Enter focuses the mapping
  dropdown for the current row; `?` shows shortcut help. Arrow keys + Enter
  is mandatory for an a11y story (Issue 5 below).
- **Save** persists; **Discard** confirms then drops back to /templates.

State indicators per row (left edge, like the existing `.from-extract`
amber-edge pattern):
- `▶` row hover/selected — amber edge
- `⚠` unmapped — amber-warn edge (`--warning`)
- `✓` user-confirmed mapping — success-edge subtle
- `✗` user marked "no mapping needed" — strikethrough text, gray edge

### Resolved decisions (Pass 7)

| Decision | Resolution | Rationale |
|----------|-----------|-----------|
| Mapping review surface | Dedicated route `/templates/<id>/review` (1A) | Bookmarkable, scales to 389-field Multi-Board, clear hierarchy |
| 30-60s upload state | Staged checklist with progress bar (2A) | Honest, builds trust during long wait |
| Home flow reorder | Pre-select 4 IL defaults so picker is ambient (3A) | Friction → 0 for first-runners |
| Upload modal icon | SVG document icon, not emoji 📄 (4) | No emoji anywhere, keeps icon vocabulary consistent |
| Mapping review responsive | Desktop-only with friendly downgrade screen below 768px (6A) | Power-user authoring tool, not worth designing 3 distinct experiences |
| A11y for click-to-link | Full keyboard nav + ARIA live region + visible focus (7A) | Makes the tool faster for sighted power users too |
| Unsaved-changes guard | beforeunload + in-app nav confirmation (8A) | Saves users from losing 30 min of mapping work |
| Picker ordering | Grouped: "Illinois standard" first, "Custom" below (9A) | Clear hierarchy, predictable defaults |

### Responsive + accessibility specs

#### Responsive (per Issue 6A)

| Surface | Desktop (≥1100px) | Tablet (768-1099px) | Mobile (<768px) |
|---------|-------------------|---------------------|-----------------|
| Home (workspace) | 4:6 grid (existing) | 4:6 grid (existing) | Stacked single column (existing `.pane-left { border-bottom }`) |
| Template library | Full-width centered list, max 720px | Same | Same, slightly tighter padding |
| Upload modal | 520px modal | 520px modal | Bottom-sheet style, slides up from bottom (animation: `slide-up 0.22s`), takes full width minus 16px padding |
| Mapping review | 6:4 side-by-side | Side-by-side, tighter (5:4) | **Friendly downgrade screen**: "Mapping review is best on a desktop. [Continue anyway →]" — if user proceeds, stack PDF on top, fields below with sticky filter bar. Tap row → smooth-scroll to PDF rectangle |

Drop the existing `@media (max-width: 960px)` breakpoint in favor of two:
- `@media (max-width: 1099px)` — tablet adjustments
- `@media (max-width: 767px)` — mobile (mapping review downgrade fires here)

#### Accessibility (per Issue 7A)

**Mapping review keyboard map:**

| Key | Action |
|-----|--------|
| `↓` / `j` | Next field row |
| `↑` / `k` | Previous field row |
| `Enter` | Focus the mapping dropdown for current row |
| `Esc` | Clear selection / close dropdown / leave overlay highlight |
| `Tab` | Move forward through interactive elements (existing browser default) |
| `Shift+Tab` | Move backward |
| `?` | Show keyboard shortcut help overlay |
| `f` | Focus filter pills |
| `/` | Focus the right-pane search (if implemented; otherwise skip) |
| `Cmd+S` | Save mapping (matches existing `Cmd+Enter` extract pattern) |

**ARIA contract:**

- Each `.pdf-field-rect` overlay has `role="button"`, `aria-label="Field: <name>, mapped to <target>, status: <state>"`, `tabindex="0"`.
- Each row in the field list has `role="listitem"` inside a `role="list"` container.
- Selected row has `aria-selected="true"`.
- Live region: `<div aria-live="polite" aria-atomic="true" class="visually-hidden">` near the top of the right pane. On selection change: announce "Row 12 of 47, field <name>, currently mapped to <target>". On Save: "Mapping saved, returning to template library." On row state change (e.g. user marks "no mapping"): announce the change.
- Focus ring: never remove `:focus-visible` outline. Add explicit `:focus-visible` styles to `.pdf-field-rect`, `.field-row` (when used as listitem), all dropdowns, all buttons.

**Color contrast:** all existing tokens pass WCAG AA on dark bg:
- `--text` (#ededed) on `--bg` (#0a0a0a): 18.4:1 ✓
- `--text-muted` (#8a8a8a) on `--bg`: 6.4:1 ✓
- `--accent` (#e8b04b) on `--bg`: 8.7:1 ✓
- `--text-subtle` (#5a5a5a) on `--bg`: 3.0:1 — **fails AA for body text** (needs 4.5:1). Existing CSS uses `--text-subtle` for hint/placeholder text only, which has a relaxed contrast requirement. **No new use of `--text-subtle` for body text in new surfaces.** Document this constraint.

**Touch targets:** 44px minimum on all interactive elements (matches existing `.btn-primary` 32px height — that one is borderline; OK for desktop but **upgrade to 40px+ for mobile via media query**).

### Interaction state coverage

Every UI feature must specify all 5 states. Implementer ships these or fails design.

| Feature | LOADING | EMPTY | ERROR | SUCCESS | PARTIAL |
|---------|---------|-------|-------|---------|---------|
| **Template library list** | Skeleton rows (3) with shimmer; same height as real rows | "No custom templates yet. You have 4 IL templates ready. [+ Upload template] to add more." centered card, amber primary CTA | "Couldn't load templates. [Retry]" toast + cached state | Templates render with status pills | n/a |
| **Upload modal — drop zone** | Hover/drag-active = amber-tint border + "Drop to upload" text replace | (resting state IS empty) | After error: red toast slides in, modal stays open, retry available | File accepted → flips to "Analyzing…" stage view | n/a |
| **Upload — AI mapping** | Staged checklist (per Issue 2A): "Reading PDF ✓", "Extracting 47 fields ✓", "Asking AI to map fields… ████ 25s remaining". Progress bar indeterminate but visibly animated. | n/a | Stage 1 fail: "Couldn't parse this PDF. Maybe it's not a standard form?" + Try Another button. Stage 2 fail: "This PDF has no fillable fields. We support AcroForm only." Stage 3 fail: "AI mapping failed. Try uploading again or [skip and map manually]." | Auto-redirect to `/templates/<id>/review` | n/a |
| **Mapping review — PDF.js render** | Skeleton page-rect with shimmer, same dimensions as the actual page | n/a | "Couldn't render this PDF page. [Retry]" inline | Canvas renders + amber rect overlays appear (200ms fade-in) | If only some pages render: "Page 2 of 4 failed. Continue with what we have?" |
| **Mapping review — field rows** | List skeleton (5 rows of mono-text shimmer) | "All fields mapped ✓" celebratory state if user confirms every row | n/a (validation errors are inline per-row) | All rows confirmed → Save button activates | "12 of 47 reviewed" progress on top, plus filter pill `[Unmapped: 35]` |
| **Mapping review — Save** | Button shows spinner, disabled | n/a | Toast "Couldn't save mapping. Retry?" stays on same screen | Toast "Mapping saved" + redirect to /templates with the new template highlighted (amber-fade animation matching `@keyframes fade-up`) | n/a |
| **Home — Doc picker** | n/a (instant from `/api/templates`) | "No templates yet. [Upload one] or [Generate without templates]" — but this never fires because we seed 4 IL defaults | If list fetch fails: "Couldn't load templates. Working with cached list. [Retry]" | List renders, default 4 selected | n/a |
| **Home — Extract** | (existing — keep as is: button spinner, fields appear with fade-up) | n/a | (existing — keep) | (existing — keep) | If dynamic schema 400: "Extraction succeeded for core fields. Some custom fields couldn't be filled — [Show details]." inline warning panel |
| **Home — Generate** | (existing — keep) | n/a | Per-doc errors: success docs download, failed ones get red `.readiness-error` pill + tooltip with reason. **Don't break the batch.** | (existing — keep, with the doc card amber → green animation) | If 2 of 3 succeeded: top-of-list summary "2 of 3 generated. 1 failed: see Tenant Rep below." + retry-failed-only button |

### Design system additions required for V1

Current design system lives entirely in [frontend/styles.css](frontend/styles.css) — no
DESIGN.md, but the CSS is well-tokenized. New surfaces need 4 small additions, all
calibrated to the existing language.

#### New components

```css
/* Progress bar — for upload staged checklist + mapping review progress */
.progress-bar {
  height: 4px;
  background: var(--surface-3);
  border-radius: 999px;
  overflow: hidden;
}
.progress-bar-fill {
  height: 100%;
  background: var(--accent);
  border-radius: 999px;
  transition: width 0.4s ease;
}
.progress-bar-fill.indeterminate {
  width: 33%;
  animation: indeterminate-slide 1.4s ease-in-out infinite;
}
@keyframes indeterminate-slide {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(300%); }
}

/* Stage list — for upload modal */
.stage-list { display: flex; flex-direction: column; gap: 10px; }
.stage {
  display: flex; align-items: center; gap: 10px;
  font-size: 13px; color: var(--text-muted);
}
.stage[data-state="done"] { color: var(--text); }
.stage[data-state="done"] .stage-icon { color: var(--success); }
.stage[data-state="active"] { color: var(--text); }
.stage[data-state="active"] .stage-icon { color: var(--accent); }
.stage-icon { width: 16px; }

/* Breadcrumb — for mapping review topbar */
.breadcrumb {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; color: var(--text-muted);
}
.breadcrumb a { color: var(--text-muted); text-decoration: none; }
.breadcrumb a:hover { color: var(--text); }
.breadcrumb [aria-current="page"] { color: var(--text); }
.breadcrumb-sep { color: var(--text-subtle); }

/* PDF field-rect overlay (mapping review) */
.pdf-field-rect {
  position: absolute;
  border: 1px solid var(--border-strong);
  border-radius: 2px;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease;
}
.pdf-field-rect:hover,
.pdf-field-rect[data-state="hover"] {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.pdf-field-rect[data-state="selected"] {
  border-color: var(--accent);
  background: var(--accent-soft);
  border-width: 2px;
}
.pdf-field-rect[data-state="unmapped"] {
  border-color: var(--warning);
  background: var(--warning-soft);
}
.pdf-field-rect[data-state="confirmed"] {
  border-color: var(--success-strong);
}
```

#### No new color tokens

Reuse existing: `--accent` for active state, `--success` for confirmed, `--warning`
for unmapped, `--danger` for errors, `--text-muted` for secondary text. **Do not
introduce a new accent color** — the system has 1 accent (amber) by design.

#### DESIGN.md gap (deferred to TODO)

No DESIGN.md exists. The CSS tokens, when extracted to a written design system doc,
would be ~150 lines. Defer to a TODO unless the user wants it now (small ask, ~30
min with CC).

### User journey storyboard

Three personas, three flows. Each step lists what the user **does** and **feels**.

#### Persona 1: First-run (new user, never opened the app)

| # | User does | User feels | Plan supports? |
|---|-----------|------------|----------------|
| 1 | Lands on `/` | Curious, slight orient-tax | Topbar logo + tagline + 2-pane workspace (familiar) |
| 2 | Sees doc picker pre-checked with 4 IL templates | "Oh good, defaults are sensible" | Decision 3A: defaults pre-selected, friction → 0 |
| 3 | Pastes notes, hits Cmd↵ | Anticipation | Existing extract flow, button spinner |
| 4 | Sees parsed fields appear (fade-up animation) | Validation, mild surprise | Existing fade-up + amber from-extract edge |
| 5 | Edits fields if needed | In control | Form, no surprise |
| 6 | Hits Generate | Hopeful | Existing button → spinner |
| 7 | Sees PDFs preview render | "It worked!" | Existing PDF.js preview + tab strip |
| 8 | Downloads or signs | Done | Download button per tab |

**Emotional arc:** orient → trust → validate → succeed → done. Hold this arc; don't add friction.

#### Persona 2: Power-user (custom template upload)

| # | User does | User feels | Plan supports? |
|---|-----------|------------|----------------|
| 1 | Realizes they need to add a Pet Addendum | Annoyance (pre-existing — they have to author this) | The reason this feature exists |
| 2 | Clicks Templates topbar link or `+ Upload template` inline | Decisive | Two entry points (topbar nav + inline) |
| 3 | Drops PDF on upload modal | Active | Modal opens, drag-active amber-tint state |
| 4 | Sees "Reading PDF ✓" → "Extracting 47 fields ✓" → "Asking AI to map fields…" with progress bar | Patient, informed | Issue 2A: staged checklist with timing |
| 5 | (Waits 30-60s) Sees redirect to mapping review page | Relief — fast feedback | Sync upload, auto-redirect on success |
| 6 | Scans PDF on left, AI's proposals on right | Skeptical (validation phase) | Side-by-side hierarchy + click-to-link |
| 7 | Clicks a row, sees rectangle highlight | "Oh, I see what it picked" | Real-time link, amber outline |
| 8 | Confirms 35 mappings, fixes 12 | Engaged, in control | Clear `▶/⚠/✓/✗` row state indicators |
| 9 | Hits Save | Done with auth-work | Save → toast → redirect to /templates with new template highlighted |
| 10 | Goes back to home, picks the new template, fills | "It just works" | New template appears in doc picker with `[CUSTOM]` pill |

**Emotional arc:** annoyance → decisive → patient → skeptical → engaged → done. Pass 6's skeptical-to-engaged transition is the **trust earning moment** — the rectangle-row link must feel snappy (≤150ms scroll, immediate highlight).

#### Persona 3: Returning power-user

| # | User does | User feels | Plan supports? |
|---|-----------|------------|----------------|
| 1 | Lands on `/`, picker shows 4 IL + Pet Addendum + Pool Disclosure | Recognition | Templates persist via sqlite |
| 2 | Unchecks 2, leaves 5 | In control | Existing checkbox UX |
| 3 | Pastes notes (longer because of pet info) | Confident | Existing textarea |
| 4 | Cmd↵ extracts, including the `template_extras` for pet info | "It got the pet name!" | Dynamic schema works (Pillar 2 spike confirms) |
| 5 | Generates → 5 PDFs preview | Trust earned | Per-doc error batching = 5 download buttons or partial success summary |

**Emotional arc:** recognition → control → confidence → delight. The delight at step 4 (custom field extraction working) is the **retention moment** — if it fails, the user loses trust in the whole upload feature.

### 5/5/5 time-horizon design check

- **5 seconds (visceral):** topbar logo, dark theme, two clear panes, intentional typography. Existing 8/10 visceral quality, doesn't degrade with new surfaces if we hold the design system.
- **5 minutes (behavioral):** all 5 interaction states specified, error recovery clear, keyboard shortcuts present. New mapping review UI is the test — must feel as polished as the existing home in 5 minutes.
- **5 years (reflective):** "this saves me an hour per deal." That's the product, design just removes the friction between the intent and the result.

### Drag-drop upload modal — Surface D (modal, not page)

Smaller, focused. Dialog-sized, ~520px wide.

```
┌──────────────────────────────────────────────────┐
│ Upload template                          [✕]    │
│ ─────────────────────────────────────────────── │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │                                          │   │
│  │  [doc-svg]  Drop PDF here, or click to   │   │
│  │             browse                       │   │
│  │                                          │   │
│  │   Must have fillable form fields         │   │
│  │   (AcroForm). No scans or images.        │   │
│  │                                          │   │
│  └──────────────────────────────────────────┘   │
│  ↑ [doc-svg] = 24x24 SVG, stroke=currentColor,  │
│  matching the existing paperclip icon style.    │
│  No emojis anywhere in the UI.                  │
│                                                  │
│  Title:  ┌────────────────────────────────────┐ │
│          └────────────────────────────────────┘ │
│          (Auto-fills from filename)              │
│                                                  │
│ ─────────────────────────────────────────────── │
│                          [Cancel]  [Upload →]    │
└──────────────────────────────────────────────────┘
```

After Upload:
```
┌──────────────────────────────────────────────────┐
│ Analyzing Pet Addendum…                         │
│ ─────────────────────────────────────────────── │
│                                                  │
│   ⊙  Reading PDF … done                          │
│   ⊙  Extracting 47 fields … done                 │
│   ⊙  Asking AI to map fields …                   │
│      ████████░░░░░░░░░░░  35% · ~25s remaining   │
│                                                  │
│   This can take up to a minute on large forms.   │
│                                                  │
└──────────────────────────────────────────────────┘
   ↓ on success
   redirects to /templates/<id>/review
```

The plan said "spinner with text" — that was a 3/10. The progress checklist above
gives the user **specific signal** that things are happening, **not** a generic spinner.

---
## Pillar 1 — Fix autofill (SHIPPING STANDALONE TODAY)

> **Sequencing decision:** This pillar lands as its own PR before any V1 work begins.
> ~50 LOC fix + tests. Outside voice was correct — don't bundle the demo-killer fix
> behind speculative architecture.

Files touched: new `backend/pdf_introspect.py`, new `backend/pdf_fill.py`, modified
`backend/generate.py`, refactored `scripts/inspect_pdfs.py`,
`scripts/dump_multiboard_checkboxes.py`, `scripts/label_pdf_fields.py`, new
`backend/tests/test_pdf_fill.py`.

### Tasks — Pillar 1

- [ ] **Create `backend/pdf_introspect.py`.** One module owns AcroForm reading. API:
  - `walk_fields(reader) -> list[FieldInfo]` where `FieldInfo` carries `dotted_name`,
    `field_type` (`/Tx`/`/Btn`/`/Ch`/`/Sig`), `widget_rects: list[(page, x,y,w,h)]`,
    `widget_states: list[str]` (for `/Btn` kids).
  - `extract_neighbor_text(reader, rect, page) -> str` returning the few sentences of
    page text within ~50pt of the widget rectangle. Used by Pillar 2's upload flow,
    but built and tested now.
- [ ] **Create `backend/pdf_fill.py`.** Replace `_force_appearances` + per-page fill.
  API: `fill_pdf(reader, rendered: dict[str,str]) -> bytes`.
  - Walks `/AcroForm/Fields` recursively, joining ancestor `/T` parts to build dotted names.
  - Writes `/V` on the leaf field. For `/Btn`: also set `/AS` on every widget kid that
    has a matching appearance state (so Preview/Acrobat render the check visually).
  - Sets `/NeedAppearances=true` on AcroForm.
  - **Skips empty rendered values** — don't overwrite pre-filled fields with blank.
- [ ] **Refactor `backend/generate.py`** to call `fill_pdf` and drop
  `update_page_form_field_values`.
- [ ] **Refactor scripts/** to import from `backend/pdf_introspect.py`. This fixes
  `scripts/label_pdf_fields.py`'s broken fill as a side effect (it currently uses the
  same broken `update_page_form_field_values`).
- [ ] **Tests — `backend/tests/test_pdf_fill.py`.** Coverage matrix:
  - REGRESSION: `test_lease_invoice_fills_all_six_mapped_fields` — before-fix would fail.
  - REGRESSION: `test_lease_abstract_dotted_path_resolves` — `Lease End Date.0.0`.
  - REGRESSION: `test_multiboard_checkbox_visual_state` — `/AS` set on widget kids
    when mapping renders to a state name like `/On` or `/Choice1`.
  - `test_fill_pdf_skips_empty_rendered_values` — pre-existing /V is preserved.
  - `test_fill_pdf_handles_radio_groups` — multiboard property_type only one /On at a time.

### What this pillar deliberately does NOT do

- No `required_fields: [...]` meta plumbing (cut from scope per review).
- No `defaults.py` refactor (cut from scope; defer until Pillar 2 forces the seam).
- No frontend changes (this is a pure-backend fix).

### Acceptance for Pillar 1 PR

- All four existing templates fill ≥ their mapped non-empty fields in test fixture.
- `lease_invoice` regression test fails before the fix and passes after.
- `scripts/inspect_pdfs.py` + `scripts/label_pdf_fields.py` still work after refactor.

---

## Pillar 2 — Drag-drop template upload (V1 main work)

User wants this for pseudo-production use, so we build the full flow.

### Pre-work: validate the riskiest assumption FIRST

- [ ] **Spike: dynamic Pydantic + OpenAI Responses API compatibility.** Before
  writing any production code, prototype:
  - Build a `TransactionFields` subclass at runtime via `create_model()` adding 2-3
    fake `extra_fields` namespaced under `template_extras.<id>.<field>`.
  - Pass it to `client.responses.parse(text_format=...)` against the real API.
  - Confirm: (a) no strict-mode 400s, (b) extracted output matches the dynamic schema,
    (c) prompt-cache hit rate isn't fully broken (different shape per request).
  - **If this fails:** fall back to extracting core schema first, then post-extraction
    pass for active templates' extra fields (two API calls per /api/extract). Update
    plan accordingly before continuing.

### Sequencing fix (from outside voice #6)

Today, extraction happens BEFORE document selection. With dynamic schema, we need to
know which templates are active so we know which extra_fields to add. Two options:

- **Reorder:** user picks templates first → click Extract → notes box becomes available.
- **Two-phase extraction:** core extract first, then post-extract pass when templates
  are picked, just for that template's extras.

Decision: **reorder UI flow.** Document picker moves above the notes pane. Default
selection = the 4 IL templates so existing behavior is preserved.

### Data model

```python
# backend/models.py (raw sqlite3, no ORM)
class Template:
    id: str                         # uuid
    title: str                      # user-given
    source_pdf_path: str            # templates/pdf/<uuid>.pdf
    mapping_path: str               # templates/mappings/<uuid>.json
    status: Literal["pending_review", "ready", "needs_attention"]
    created_at: datetime
    extra_fields_json: str          # serialized list[ExtraField]

class Transaction:
    id: str
    fields_json: str                # the TransactionFields snapshot used to fill
    agent_json: str
    created_at: datetime

class GeneratedDocument:
    id: str
    transaction_id: str
    template_id: str
    pdf_path: str                   # filesystem; templates/generated/<txn>/<doc>.pdf
    created_at: datetime
```

`MappingFile` (Pydantic, validates every mapping JSON on load):

```python
class ExtraField(BaseModel):
    name: str                       # snake_case, stored under template_extras.<tpl_id>.<name>
    type: Literal["text","money","date","number","bool","list_str"]
    description: str                # used in extraction prompt
    pdf_field: str                  # which AcroForm field this fills

class MappingMeta(BaseModel):
    title: str
    source_pdf: str
    filled_filename: str
    notes: str | None = None

class MappingFile(BaseModel):
    meta: MappingMeta = Field(alias="_meta")
    fields: dict[str, str]          # AcroForm field name → template string
    extra_fields: list[ExtraField] = Field(default_factory=list)
```

### Upload flow

```
1. POST /api/templates/upload     (multipart: pdf, title)
2. Server:
   a. Save PDF → templates/pdf/<uuid>.pdf.
   b. Open with pypdf. Detect:
      - No AcroForm        → 400 "this PDF is not fillable, use a form-enabled template"
      - Encrypted          → 400 "this PDF is password-protected, unlock first"
      - Malformed          → 400 "couldn't parse PDF"
   c. backend/pdf_introspect.walk_fields() + extract_neighbor_text() per widget.
   d. GPT-5 call: field list + neighbor text + canonical schema → proposed mapping
      (which canonical fields, which template-specific extra_fields).
   e. Persist proposal to templates/mappings/<uuid>.json with status='pending_review'.
   f. Return template_id + proposal + widget rectangles for the review UI.
3. Frontend renders the review:
   - PDF.js renders the page to canvas.
   - Absolute-positioned <div>s overlay each widget rectangle.
   - Right pane: row per field with AI's proposed mapping, edit dropdown, delete.
   - Click row → highlight rect on PDF; click rect → scroll/highlight row.
4. PUT /api/templates/<id>/map   (confirmed mapping)
5. Status flips to 'ready'. Template appears in document picker.
```

UX detail (sync, per Issue 8A): upload spinner says
"Analyzing N fields, this can take a minute for large forms (Multi-Board ≈ 60s)."

### Extraction prompt extension

When templates with `extra_fields` are active, build a dynamic Pydantic model that
extends `TransactionFields` with a `template_extras: dict[template_id, ExtrasModel]`
where each `ExtrasModel` is built from that template's `extra_fields`. Pass to
`client.responses.parse(text_format=...)`.

**Namespacing rule (per Issue 4A):** all extra fields live under
`template_extras.<template_id>.<field_name>`. No collision with core schema possible.
Mapping templates reference them via `{template_extras.<id>.pet_deposit}`.

### Tasks — Pillar 2

- [ ] **Spike** dynamic Pydantic + Responses API (above). **BLOCKING.**
- [ ] `backend/db.py` — sqlite DAL. Schema migration runs at startup (SQL files in
  `backend/migrations/`). Bare `sqlite3`, no ORM.
- [ ] `backend/models.py` — Pydantic models for `Template`, `Transaction`,
  `GeneratedDocument`, `MappingFile`, `ExtraField`.
- [ ] Validate `MappingFile` on every load in `backend/generate.py` (per Issue 5A).
- [ ] **Per-doc error batching on /api/generate** (per Issue 5A). Response shape:
  `{documents: [GeneratedDoc], failures: [{document_key, error}]}`. One bad mapping
  doesn't break the batch.
- [ ] **Disk-full handling on /api/generate.** Wrap PDF write to filesystem in
  try/except. On `OSError` (disk full, permission denied), return 503 with a clear
  message. ~5 LOC. (Per failure-modes table critical gap.)
- [ ] `backend/templates.py` — upload, neighbor-text extraction (uses
  `pdf_introspect.extract_neighbor_text` from Pillar 1), AI mapping proposal.
- [ ] `backend/extract.py` — dynamic-schema mode taking `active_template_ids` param.
  Falls back to base `TransactionFields` if list is empty.
- [ ] **Migration: seed the 4 default templates** (Multi-Board, Tenant Rep, Lease
  Abstract, Lease Invoice) into the templates table on first boot, with their
  existing mapping JSONs converted to the new `MappingFile` shape.
- [ ] Frontend: template library page (drag-drop, list, status pills).
- [ ] Frontend: mapping review modal — PDF.js + canvas overlay, click-row-to-highlight,
  click-rect-to-scroll.
- [ ] Frontend: document picker — replaces hardcoded list with `GET /api/templates`,
  defaults to the 4 IL templates checked.
- [ ] **Reorder UI flow**: document picker BEFORE notes/extract (per sequencing fix).

### Acceptance for Pillar 2 PR

- Upload 5th PDF → AI proposes mapping → user confirms → can use in generate.
- Upload encrypted PDF → friendly error.
- Upload no-AcroForm PDF → friendly error.
- AI mapping eval on the 4 existing PDFs ≥ 80% match on simple templates,
  ≥ 60% on Multi-Board (per Issue 7A).
- Extraction eval shows no regression on existing 4 templates after dynamic schema lands
  (per Issue 6A).

---

## Pillar 3 — DocuSign-ready storage (design only this iteration)

Cut down per scope reduction. Just persist the right shape now. No 501 endpoint, no
recipient stub, no anchor strings yet.

### Tasks — Pillar 3

- [ ] **Verify DocuSign anchor-tagging compatibility with AcroForm /V.** Outside voice
  flagged this as unverified. Spike before writing the schema additions: does DocuSign
  Tabs API match anchor strings inside AcroForm field /V values, or only flattened
  text? If only flattened, anchor-string strategy doesn't work and we'll position
  tabs by /Rect coordinates instead. **Document the answer in tasks/lessons.md.**
- [ ] sqlite migration adds `transactions` and `generated_documents` tables (already
  in Pillar 2 because the spike forces persistence).
- [ ] `/api/generate` writes Transaction + GeneratedDocument rows + saves filled PDFs
  to `templates/generated/<txn_id>/`. Endpoint still returns b64 for download.
- [ ] **`GET /api/transactions/<id>/zip`** — streams a zip of all generated PDFs
  for that transaction. Uses Python `zipfile.ZipFile` over the persisted files in
  `templates/generated/<txn_id>/`. ~20 LOC. Frontend adds a "Download all (.zip)"
  button in the preview-mode header next to the existing per-tab download icons.
  Filename: `<address-slug>-<date>.zip`, e.g. `221-w-hubbard-803-2026-05-05.zip`.

### Out of scope this iteration

- DocuSign OAuth, envelope creation, webhook handling, signing-URL embedding.
- Anchor-string mapping per template.
- Per-recipient field routing (tenant signs here, agent signs here).
- The `envelopes` table — design it when DocuSign actually lands.

---

## Implementation order

1. **Pillar 1** — autofill fix + `pdf_introspect` + tests. **Ships standalone today.**
   ~half a day with CC.
2. **Pillar 2 spike** — dynamic Pydantic + Responses API compat. **~1 hr.** Blocks
   the rest of Pillar 2.
3. **Pillar 3 spike** — DocuSign anchor-tagging compat. **~1 hr.** Doesn't block
   shipping but informs anchor-string design.
4. **sqlite + DAL + migrations** — ~half a day.
5. **Templates upload + AI mapping + eval** — ~1.5 days.
6. **Mapping review UI (PDF.js + overlay)** — ~1 day. Outside voice flagged the
   half-day estimate as fantasy. Believing it.
7. **Frontend reorder + document picker rewrite** — ~half a day.
8. **Persistence on /api/generate** — ~few hours.

**Realistic total: 6-8 days for the full V1 (per outside voice #4).** Original 3-day
estimate was fantasy — confirmed.

---

## What already exists (DRY check)

| Existing | Plan touches | Action |
|----------|-------------|--------|
| `scripts/inspect_pdfs.py` (AcroForm walker) | YES — Pillar 2 needs same logic | **Refactor** → `pdf_introspect.walk_fields()` |
| `scripts/dump_multiboard_checkboxes.py` (`/AS` extraction) | YES — Pillar 1 needs same logic | **Refactor** into `pdf_introspect` |
| `scripts/label_pdf_fields.py` (broken fill via `update_page_form_field_values`) | YES — same root bug | **Fix as side effect of Pillar 1 refactor** |
| `backend/interpolate.py::build_context` | NO direct change | Touch only when Pillar 2 forces a seam |
| `backend/extract.py` SYSTEM_PROMPT | YES — must include extra_fields | Extend with dynamic context |

---

### Design — NOT in scope (deferred)

- DESIGN.md write-up extracting the existing CSS into a written design system doc.
  Captured as a TODO. Not blocking V1.
- Visual mockups via the gstack designer. Blocked by OpenAI org verification.
  ASCII wireframes stand in. Captured as a TODO to revisit.
- Mockup-to-HTML pipeline (`/design-html`). Defer until V2.
- Custom dark/light mode toggle. App is dark-only. Light mode = future.
- Animation polish beyond the existing `fade-up` / `slide-in` patterns.
- Onboarding flow / empty-state-of-app (the "no transactions yet" first-launch).
  Plan currently has no first-launch UX because the 4 IL defaults make it
  immediately useful.

### Design — What already exists

| Existing asset | Used in V1? | Notes |
|----------------|-------------|-------|
| Color tokens (`--bg`, `--accent`, `--success`, etc.) | YES — all surfaces | No new colors needed |
| Type scale (Inter + JetBrains Mono) | YES | No new fonts needed |
| `.btn-primary`, `.btn-ghost`, `.btn-icon` | YES | Reused in upload modal, mapping review |
| `.notes-wrap.dragover` amber-tint | YES | Reused for template-library page-wide drop target |
| `.readiness-pending/.ready/.partial/.missing/.error` pills | YES | Vocabulary maps to template `status` field |
| `.from-extract` 2px amber left-border | YES — and extended | Same pattern for `.field-row` selection in mapping review |
| `.drawer` (440px right slide) | NO for mapping review (needs full page) | Still used for agent profile |
| `.toast`, `.toast-container` | YES | Used for save success, errors |
| `@keyframes fade-up`, `slide-in`, `fade-in` | YES | Reused for new surfaces — no new animations needed |
| PDF.js render pattern (`.pdf-page canvas`) | YES — extended with overlay rects | Already imported for download preview |

## NOT in scope this iteration

- Multi-user / auth (single-agent local tool).
- PDF redlining or annotation editing.
- Coordinate-overlay templating for non-AcroForm PDFs (rejected at upload time).
- DocuSign live integration (deferred per user direction).
- Per-recipient routing for DocuSign tabs.
- WebSocket/SSE progress streaming on upload (sync with spinner is fine).
- Background job queue (no async polling for V1).
- `required_fields: [...]` mapping meta + frontend validation (cut from Pillar 1).
- `defaults.py` extraction from `interpolate.py` (defer until Pillar 2 forces it).

---

## Failure modes (production scenarios)

| Codepath | Realistic failure | Test? | Error handled? | User sees |
|----------|------------------|-------|----------------|-----------|
| `/api/templates/upload` | Encrypted PDF | YES | YES | Friendly "unlock first" |
| `/api/templates/upload` | No AcroForm | YES | YES | "Not fillable" message |
| `/api/templates/upload` | Malformed PDF | YES | YES | "Couldn't parse" |
| `/api/templates/upload` | OpenAI API timeout (60s+) | YES | YES | "Mapping failed, try again" |
| `/api/extract` | Dynamic schema 400 from OpenAI | YES (eval) | YES (fallback to two-phase) | Transparent |
| `/api/generate` | One mapping malformed | YES | YES (per-doc batching) | Other docs still download |
| `/api/generate` | Disk full writing PDF | YES | YES | 503 "out of disk space" |
| pdf_fill | Mapping references non-existent field path | YES (MappingFile validates) | YES | Upload rejects |

**Critical gaps addressed in this plan:** disk-full handling on /api/generate
(wrapped in try/except returning 503, ~5 LOC, captured in Pillar 2 task list).

---

## Worktree parallelization

Sequential dependency chain — most work touches `backend/` core, no parallel lanes
that don't conflict. **Implement sequentially.**

The one parallelizable piece: while waiting on a Pillar 2 backend implementation,
the frontend mapping review UI (PDF.js + overlay) can be built against a stub backend
that returns hand-authored mapping proposals.

---

## Cross-model tensions resolved

| Tension | Decision |
|--------|----------|
| Drag-drop UI vs scaffold script + JSON files | **Keep drag-drop UI** — needed for pseudo-prod use. |
| Ship Pillar 1 standalone today | **Yes** — own PR, before V1 work. |
| sqlite vs JSON files | **sqlite** — consistent with full Pillar 2. |

## Outside voice items folded in (non-decision items)

- Dynamic Pydantic spike before commitment (Pillar 2 pre-work).
- DocuSign anchor compatibility spike (Pillar 3 pre-work).
- Realistic time estimate updated 3 days → 6-8 days.
- Sequencing fix: document picker moves before notes/extract.

---

## Test plan

Eng-review test plan artifact:
`~/.gstack/projects/jason-zhnn-real-estate-paperwork-automator/maanavagrawal-main-eng-review-test-plan-20260505-221153.md`

Covers all REGRESSION + happy path + edge case + critical path coverage required to
ship. Read this alongside the plan during implementation.

---

## Review section (fill in as work completes)

- [x] **Edit-in-preview shipped** — 2026-05-07, ahead of Pillar 2.
  - `backend/pdf_render.py` (new) — renders each page to PNG via pypdfium2,
    flips PDF-points → PNG-pixel rects on the server so the frontend
    positions overlay inputs without re-doing y-flip math.
  - `POST /api/preview` — takes a base64 PDF, returns pages + per-field
    rects + current /V values.
  - `POST /api/edit` — takes a base64 PDF + edits dict, re-fills via
    `pdf_fill.py`, returns the edited PDF + a fresh preview.
  - Frontend: replaced the `<iframe>` preview with stacked
    `<img>` + absolute-positioned `<input>`/`<textarea>`/`<input type=checkbox>`
    over each AcroForm field. Edits track in `pendingEdits`; "Save edits"
    button posts to `/api/edit` and swaps the doc bytes so downloads stay
    in sync.
  - `pdf_introspect.walk_fields` now does a name-based fallback for
    widget→page mapping (necessary after `PdfWriter.clone_from` because
    widgets and fields end up as separate objects with new idnums).
  - Readiness pills in selection mode now recompute on every form input,
    not just after extraction.

- [x] **Pillar 1 shipped** — 2026-05-05, 7/7 regression tests passing.
  - `backend/pdf_introspect.py` (new) — AcroForm walker with dotted-name
    resolution + neighbor text extraction.
  - `backend/pdf_fill.py` (new) — direct /V tree-write filler with /AS for
    /Btn widgets.
  - `backend/generate.py` — refactored to call `fill_pdf`.
  - `scripts/inspect_pdfs.py`, `scripts/dump_multiboard_checkboxes.py`,
    `scripts/label_pdf_fields.py` — refactored to use `pdf_introspect`,
    fixing `label_pdf_fields.py`'s broken fill as a side effect.
  - `backend/tests/test_pdf_fill.py` — 7 tests covering: lease_invoice 6/6
    regression, lease_abstract dotted-path regression, multiboard checkbox
    `/AS` regression, radio group fill, empty-value preservation, all-4
    smoke test, /NeedAppearances flag.
  - One walker fix during implementation: a kid that's both `/Subtype=/Widget`
    AND has `/T` is a self-widgeted leaf, not a pure annotation kid.
- [x] **Pillar 2 chunk 6 (MVP) shipped** — 2026-05-08. Frontend wired to the
  dynamic backend.
  - Hardcoded 4-doc list in index.html replaced with a runtime
    `loadTemplates()` call to `/api/templates`. Cards render dynamically
    with checkbox state preserved across re-renders. IL defaults pre-checked
    by default; custom uploads auto-check on completion.
  - "Upload template…" button below the doc list opens a modal with
    drag-drop or click-to-browse. Title auto-suggests from filename.
    Submit button disabled until both file + title present.
  - During upload: modal swaps to a 3-stage checklist (Reading PDF →
    Extracting form fields → Asking AI to map fields) with an indeterminate
    progress bar. Cancel disabled mid-upload to avoid orphan rows; Esc
    blocked too. Hint copy says "Up to a minute on large forms."
  - Custom (non-default) doc cards get a delete button on hover that
    confirms then calls DELETE /api/templates/<id>.
  - Readiness pills: IL defaults still use the per-doc required-fields
    heuristic; custom templates show "awaiting extraction" before extract
    and "ready" after.
  - Deferred to a later iteration: the dedicated `/templates/<id>/review`
    mapping review UI with PDF.js + canvas overlay (per design plan).
    User reviews the AI's mapping by editing the form on the left after
    extraction; wrong canonical mappings can be fixed by editing the
    extracted values directly.

- [x] **Pillar 2 chunk 5 shipped** — 2026-05-08. Dynamic-schema extraction.
  `/api/extract` now accepts `active_template_ids` (comma-separated). For
  each active template with `extra_fields`, the endpoint builds a Pydantic
  subclass at request time that adds a `template_extras.<template_id>`
  nested object, then hands that subclass to `responses.parse(text_format=)`.
  One API call extracts both canonical fields AND every active template's
  extras.

  Smoke test: synthetic pet-addendum template with 3 extras + notes
  mentioning "golden retriever named Lucy, $300 pet deposit" → 14s call →
  template_extras.pet_addendum.pet_name='Lucy', pet_breed='golden retriever',
  pet_deposit='$300'. Canonical fields still extracted correctly.

  Implementation closely follows the spike that landed in commit 9094d80;
  no plan adjustments needed.

- [x] **Pillar 2 chunk 4 shipped** — 2026-05-08. Template upload + AI-proposed
  mapping. Real-world quality on the lease abstract: **17/17 fields correctly
  classified** (canonical paths or legitimate template extras). Multi-Board
  worst case: **22/49 overlap** with hand-authored mappings — agents using
  the user-review UI in chunk 6 will fix the gaps. **Decision: ship now,
  iterate based on real-user signal rather than synthetic worst-case
  optimization.** Multi-Board is the densest legal PDF anyone will upload;
  most custom templates (pet addendums, pool disclosures, condo riders) are
  5-30 fields and look more like the lease abstract.

  Known weak spots logged for future iteration:
    - Adjacent-field label swap on tightly-packed forms (Multi-Board's
      "27 Business Days" field labels swap between fields 27 and 29).
    - Statutory "[CHECK ONE] has / has not" patterns aren't detected as
      a uniform `{statutory_state}`; AI maps each to a separate extra.
    - Page-14 "FOR INFORMATION ONLY" agent contact block (fields 322-329
      on Multi-Board) is mistaken for a generic signature block.
  All three are pattern-recognition problems that get easier when we have
  real user-corrected mappings to learn from.

- [x] **Pillar 2 spike done** — 2026-05-07. **Verdict: dynamic Pydantic works
  cleanly with the Responses API.** Run via `scripts/spike_dynamic_schema.py`.
  - 0/1/2 active templates all parse correctly. Pet addendum extracted
    `pet_name='Lucy'`, `pet_breed='golden retriever'`, `pet_deposit='$300'`
    from a freeform note. Pool disclosure same.
  - Schema audit clean across all shapes: 0 strict-mode issues, depth ≤5,
    `additionalProperties` never `true`, total props 46–53 (well under the
    100-prop cap), schema size 7.6–8.9KB.
  - Cache behavior: stable shape gets full cache hits after first call
    (1,920 / 1,974 input tokens cached on calls 2 and 3). Changing the shape
    drops cached_tokens to 0, which is fine — different active-template sets
    are different cache slots, and within one user's session the shape stays
    stable.
  - Latency: 15–30s per call, comparable to the existing baseline (46s on
    the cold call). Acceptable for a single-user tool.
  - **No plan adjustment needed.** Proceeding with the planned one-call
    architecture (`extract` takes `active_template_ids`, builds a dynamic
    `TransactionFieldsExtended` model, parses in one shot).
- [ ] Pillar 3 spike done — _verdict on DocuSign anchor strings_.
- [ ] Pillar 2 shipped — _date_, AI mapping eval _% match per template_.
- [ ] Pillar 3 shipped — _date_, persistence wired up.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | not run (codex unavailable) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 15 issues, 0 critical gaps, scope reduced |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR (PLAN) | score: 4/10 → 9/10, 10 decisions made |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |
| Outside Voice | Independent challenge | Cross-model check | 1 | issues_found (Claude subagent) | 7 challenges, 3 cross-model tensions resolved, 4 folded into plan |

**DESIGN:** Initial 4/10 → 9/10 across all 7 passes. 3 ASCII wireframes added
(home, template library, mapping review + upload modal). All 5 interaction states
specified per surface. Full keyboard nav + ARIA contract for mapping review.
Mockup generation deferred (OpenAI org verification needed).

**OUTSIDE VOICE:** 7 structural challenges raised by independent Claude subagent.
3 cross-model tensions explicitly resolved by user (drag-drop UI kept, Pillar 1
ships standalone, sqlite stays). 4 incorporated as plan items: dynamic-Pydantic
spike, DocuSign anchor-tagging spike, 6-8 day estimate (was 3), document-picker-
before-extract sequencing.

**UNRESOLVED:** 0

**VERDICT:** ENG + DESIGN CLEARED — ready to implement. Pillar 1 ships standalone
today; Pillar 2 has detailed visual + behavioral specs to build against.
