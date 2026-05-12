// Chip strip — Console direction (plan-design-review approved).
//
// The chip strip is a *view* over the same field state owned by the existing
// parsed-fields accordion. There is one source of truth (the data-path inputs
// in <section id="fields">); chips read from those inputs on render and write
// back to them on edit. That way Save edits, runGenerate, and the accordion
// keep working without changes.
//
// Visual language (from /design-shotgun "Console" direction):
//   - rectangular tokens, 6px radius, JetBrains Mono values
//   - 2px left state bar:  transparent (default), amber (low-confidence),
//                          neutral (user-edited)
//   - flat stone-100 surface, single amber accent
//   - tap = transform in place to <input>; Enter saves; Esc cancels; blur saves
//
// State is held in chipState[path] = {value, source, confidence, edited}.
// Rendering is full-replace per call — simple, fast enough for ~20 chips.

import { setByPath, getByPath, getDefaults, defaultsHas } from "./defaults.js";

// Canonical chip order. Keep in sync with backend/main.py _CHIP_FIELDS.
const CHIP_ORDER = [
  { path: "property.address",       label: "Address" },
  { path: "property.unit",          label: "Unit" },
  { path: "transaction_type",       label: "Type" },
  { path: "purchase_price",         label: "Price" },
  { path: "monthly_rent",           label: "Rent" },
  { path: "earnest_money",          label: "Earnest" },
  { path: "closing_date",           label: "Closing" },
  { path: "lease_start",            label: "Lease start" },
  { path: "lease_end",              label: "Lease end" },
  { path: "tenant_or_buyer_names",  label: "Buyer/Tenant" },
  { path: "seller_names",           label: "Seller" },
  { path: "loan_type",              label: "Loan" },
  { path: "loan_rate_type",         label: "Rate" },
  { path: "loan_percent_of_price",  label: "LTV %" },
  { path: "loan_amortization_years", label: "Term" },
  { path: "escrowee",               label: "Escrowee" },
  { path: "commission_amount",      label: "Commission" },
  { path: "county",                 label: "County" },
];

// In-memory chip state. Keyed by canonical path. Empty by default — first
// extraction populates it, subsequent edits update it.
const chipState = new Map();

let _container = null;
let _onChipEdit = null;  // callback into app.js: (path, newValue) => void

// ---- public API ----

export function initChips(container, { onChipEdit }) {
  _container = container;
  _onChipEdit = onChipEdit;
  render();
}

export function clearChips() {
  chipState.clear();
  render();
}

/**
 * Apply one chip event from the SSE stream. Source comes from the server:
 * "extracted" or "default". Confidence is reserved for a future change —
 * for now everything that lands here is treated as high-confidence.
 */
export function applyChipEvent({ path, label, value, source }) {
  if (!path || !value) return;
  chipState.set(path, {
    label,
    value,
    source: source || "extracted",
    edited: false,
  });
  render();
}

/**
 * Called after the SSE 'done' event. Reconciles the chip strip against the
 * full payload — any chip-eligible field that has a value in the payload
 * but no chip event landed for it gets a chip too (defensive).
 */
export function reconcileFromPayload(payload) {
  for (const { path, label } of CHIP_ORDER) {
    const value = getByPath(payload, path);
    const display = formatChipValue(value);
    if (!display) continue;
    if (!chipState.has(path)) {
      chipState.set(path, { label, value: display, source: "extracted", edited: false });
    }
  }
  render();
}

/**
 * Mirror an accordion-side edit into the chip strip. Called by app.js when
 * the user types in the parsed-fields accordion so both views stay in sync.
 */
export function updateChipFromField(path, value) {
  const display = formatChipValue(value);
  const existing = chipState.get(path);
  if (!display) {
    if (existing) {
      chipState.delete(path);
      render();
    }
    return;
  }
  const label = existing?.label || CHIP_ORDER.find((c) => c.path === path)?.label || path;
  chipState.set(path, {
    label,
    value: display,
    source: existing?.source || "extracted",
    edited: true,
  });
  render();
}

// ---- internal ----

function formatChipValue(v) {
  if (v == null || v === "") return "";
  if (Array.isArray(v)) {
    const parts = v.map((x) => String(x).trim()).filter(Boolean);
    if (parts.length === 0) return "";
    if (parts.length === 1) return parts[0];
    if (parts.length === 2) return `${parts[0]} & ${parts[1]}`;
    return parts.join(", ");
  }
  return String(v).trim();
}

function render() {
  if (!_container) return;

  const chipsInOrder = [];
  for (const { path, label } of CHIP_ORDER) {
    const state = chipState.get(path);
    if (!state) continue;
    chipsInOrder.push({ path, label, ...state });
  }

  if (chipsInOrder.length === 0) {
    _container.innerHTML = `<p class="chip-strip-hint">Chips appear as you describe the deal.</p>`;
    _container.hidden = false;
    return;
  }

  _container.hidden = false;
  const defaultsKnown = getDefaults();
  const chipsHtml = chipsInOrder.map((c) => renderChip(c, defaultsKnown)).join("");
  const editedCount = chipsInOrder.filter((c) => c.edited).length;
  const meta = `${chipsInOrder.length} field${chipsInOrder.length === 1 ? "" : "s"}`
              + (editedCount ? ` · ${editedCount} edited` : "");

  _container.innerHTML = `
    <div class="chip-strip-meta">${escapeHtml(meta)}</div>
    <div class="chip-strip-list">${chipsHtml}</div>
  `;

  // Wire click-to-edit on each chip button.
  _container.querySelectorAll(".chip[data-path]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      enterEditMode(btn);
    });
  });
}

function renderChip(c, defaultsKnown) {
  const isDefault = c.source === "default";
  const isEdited = c.edited;
  const canSaveAsDefault = defaultsHas(c.path);
  // 2px left bar state class. Order matters: edited > default > extracted.
  const stateClass = isEdited ? "chip-edited" : isDefault ? "chip-default" : "chip-extracted";

  // Mark a chip indicator (small unicode glyph) — tick for default, check for edited.
  let indicator = "";
  if (isDefault) indicator = `<span class="chip-indicator chip-indicator-default" title="From your defaults">▎</span>`;
  else if (isEdited) indicator = `<span class="chip-indicator chip-indicator-edited" title="Edited">✓</span>`;

  // Always visible on eligible chips, faded at rest, brightens on hover/focus.
  // Tooltip names the field and value so the user knows what they're committing to
  // BEFORE they click — no surprise side-effects. Bookmark glyph reads more like
  // "save this" than the "+" did.
  const saveTitle = `Save "${c.value}" as your default ${c.label.toLowerCase()}`;
  const saveBtn = canSaveAsDefault && !isDefault && !isEdited
    ? `<span class="chip-save-default" data-save-default-for="${escapeAttr(c.path)}" data-save-value="${escapeAttr(c.value)}" data-save-label="${escapeAttr(c.label)}" role="button" tabindex="0" aria-label="${escapeAttr(saveTitle)}" title="${escapeAttr(saveTitle)}">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
      </span>`
    : "";

  return `
    <button type="button" class="chip ${stateClass}" data-path="${escapeAttr(c.path)}">
      <span class="chip-label">${escapeHtml(c.label)}</span>
      <span class="chip-value">${escapeHtml(c.value)}</span>
      ${indicator}
      ${saveBtn}
    </button>
  `;
}

function enterEditMode(chipBtn) {
  const path = chipBtn.dataset.path;
  const state = chipState.get(path);
  if (!state) return;

  // Replace the button content with an input. Keeping the same wrapper means
  // the chip's left bar / state class stays consistent during the edit.
  const labelHtml = `<span class="chip-label">${escapeHtml(state.label)}</span>`;
  chipBtn.innerHTML = `${labelHtml}<input class="chip-edit-input" type="text" value="${escapeAttr(state.value)}" autocomplete="off" />`;
  const input = chipBtn.querySelector(".chip-edit-input");
  input.focus();
  input.select();

  // Use a flag so the blur handler doesn't fire after Esc cancels.
  let cancelled = false;

  const commit = () => {
    if (cancelled) return;
    const newVal = input.value.trim();
    if (newVal && newVal !== state.value) {
      // Notify the host — app.js writes it back into the accordion input
      // (the canonical source of truth), then updateChipFromField echoes
      // it into the strip. Two-way sync, one direction at a time.
      _onChipEdit && _onChipEdit(path, newVal);
    } else {
      // No-op: just re-render to remove the input.
      render();
    }
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancelled = true;
      render();
    }
  });
  input.addEventListener("blur", commit);

  // Prevent the parent button's click handler from re-firing while editing.
  input.addEventListener("click", (e) => e.stopPropagation());
}

// ---- tiny safety helpers (no innerHTML XSS) ----

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
function escapeAttr(s) {
  return escapeHtml(s);
}
