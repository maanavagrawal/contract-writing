// Frontend logic — wires the dashboard to the FastAPI backend, with an
// in-place PDF preview after generation.
//
// Flow:
//   notes + images → debounced live-extract stream → chip strip populates
//                 → click Extract or hit Cmd+Enter → full extraction → accordion
//   parsed fields + agent profile + checked docs → POST /api/generate
//     → right pane swaps from selection-mode to preview-mode with one tab
//        per generated doc and an <iframe> render of the active doc
//   "← Edit selection" returns to selection-mode (cache preserved)
//
// Why <iframe> over PDF.js: pypdf sets /V on the AcroForm but doesn't generate
// appearance streams. PDF.js's canvas render shows blank fields; manually
// overlaying the annotation layer mis-aligns. Browser-built-in PDF viewers
// (Chrome, Safari, Firefox) honor /NeedAppearances=true and render filled
// values correctly with zero extra code.

import {
  initChips,
  clearChips,
  applyChipEvent,
  reconcileFromPayload,
  updateChipFromField,
} from "/modules/chips.js";
import { initVoice } from "/modules/voice.js";
import { initDefaults, saveAsDefault, removeDefault, isPathDefaulted } from "/modules/defaults.js";

// Templates the user has uploaded. IMPLEMENTED_DOCS is the live set used
// when validating which keys can be passed to /api/generate.
const IMPLEMENTED_DOCS = new Set();

// Friendly title cache, populated as templates load. Used for tab labels
// and toasts. Empty until the user uploads templates — there are no shared
// defaults in multi-tenant mode.
const FRIENDLY = {};

// Last fetched template list (lightweight). Used by the doc-card renderer
// + delete handler.
let knownTemplates = [];

const els = {
  notesWrap: document.getElementById("notes-wrap"),
  notes: document.getElementById("notes"),
  attachments: document.getElementById("attachments"),
  attachBtn: document.getElementById("attach-btn"),
  fileInput: document.getElementById("file-input"),
  extractBtn: document.getElementById("extract-btn"),
  fields: document.getElementById("fields"),
  fieldsStatus: document.getElementById("fields-status"),
  saveFieldsBtn: document.getElementById("save-fields-btn"),
  generateBtn: document.getElementById("generate-btn"),
  generateSummary: document.getElementById("generate-summary"),
  profileBtn: document.getElementById("profile-btn"),
  profileSave: document.getElementById("profile-save"),
  drawer: document.getElementById("drawer"),
  toastContainer: document.getElementById("toast-container"),
  // preview mode
  selectionMode: document.getElementById("selection-mode"),
  previewMode: document.getElementById("preview-mode"),
  backBtn: document.getElementById("back-to-selection"),
  regenerateBtn: document.getElementById("regenerate-btn"),
  saveEditsBtn: document.getElementById("save-edits-btn"),
  saveEditsLabel: document.getElementById("save-edits-label"),
  tabStrip: document.getElementById("tab-strip"),
  pagesScroll: document.getElementById("pages-scroll"),
  pagesLoading: document.getElementById("pages-loading"),
  uncertainFields: document.getElementById("uncertain-fields"),
  // extract progress UI
  extractProgress: document.getElementById("extract-progress"),
  extractProgressTip: document.getElementById("extract-progress-tip"),
  extractProgressElapsed: document.getElementById("extract-progress-elapsed"),
  // pillar 2: dynamic doc list + upload modal
  docList: document.getElementById("doc-list"),
  docListLoading: document.getElementById("doc-list-loading"),
  uploadTemplateBtn: document.getElementById("upload-template-btn"),
  uploadModal: document.getElementById("upload-modal"),
  uploadDrop: document.getElementById("upload-drop"),
  uploadFileInput: document.getElementById("upload-file-input"),
  uploadDropPrimary: document.getElementById("upload-drop-primary"),
  uploadTitle: document.getElementById("upload-title"),
  uploadForm: document.getElementById("upload-form"),
  uploadProgress: document.getElementById("upload-progress"),
  uploadSubmit: document.getElementById("upload-submit"),
  uploadCancel: document.getElementById("upload-cancel"),
  stageExtractLabel: document.getElementById("stage-extract-label"),
};

let attachedImages = []; // File[]
// True ONLY after the explicit Extract button (full-tier gpt-5). NOT set by
// the live-tier chip stream — chips are a preview, the user still has to
// click Extract for the canonical accordion+readiness state.
let extracted = false;

// Preview-mode state
let lastGenerated = []; // last /api/generate response (array of {document, filename, base64, content_type})
let activeDocKey = null;
// Per-doc preview state (image renders + pending edits) lives in docState below.

// ---------- generate state machine ----------
// The Generate button is a 3-state machine derived from data, not toggled by hand:
//   idle      — nothing to do (no extract yet, no docs picked, or already generated
//               with no changes since); button is muted/disabled or shows "View"
//   view      — successful generate exists AND inputs match the snapshot AND there
//               are no inline preview edits dirty → clicking re-enters preview mode
//               instead of paying the API call again
//   regenerate — inputs differ from snapshot (or last gen had failures, or inline
//               edits dirty) → clicking fires /api/generate
//
// `lastGenerateSnapshot` is the canonical hash of {fields, agent, sortedDocKeys}
// captured at the moment of the last successful (0-failure) generate. If any of
// those three change, the snapshot won't match and we flip to 'regenerate'.
// `previewEditsDirty` is set when the user edits a field directly on the rendered
// PDF overlay; those edits are lost on regenerate, so we warn before allowing it.
// `fieldsPendingEdits` buffers edits in the parsed-fields panel: while true, the
// generate bar does NOT recompute on every keystroke. Only an explicit "Save
// edits" click in that panel publishes the edits to the bar, so the View →
// Regenerate flip is a deliberate gesture rather than mid-typing flicker.
let lastGenerateSnapshot = null;
let previewEditsDirty = false;
let fieldsPendingEdits = false;

function currentSnapshot() {
  // Stable JSON of every input that affects /api/generate output. Sorting the
  // doc-keys array means re-checking the same docs in a different order doesn't
  // count as a change. Object keys are recursively sorted so nested-object key
  // order (which depends on DOM iteration order) doesn't perturb the hash.
  // Note: JSON.stringify's allowlist replacer (an array second arg) only
  // filters TOP-level keys — it doesn't recurse — so we hand-roll the sort.
  const fields = collectFields();
  const agent = readProfileFromInputs();
  const docs = checkedDocKeys().slice().sort();
  const stable = (v) => {
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(stable).join(",") + "]";
    const keys = Object.keys(v).sort();
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + stable(v[k])).join(",") + "}";
  };
  return stable({ f: fields, a: agent, d: docs });
}

function computeGenerateState() {
  const selected = checkedDocKeys();
  if (!extracted || selected.length === 0) return "idle";
  if (lastGenerateSnapshot && lastGenerateSnapshot === currentSnapshot() && !previewEditsDirty) {
    return "view";
  }
  return "regenerate";
}

// ---------- auth helpers ----------
// Wrap fetch so any 401 surfaces as "session gone, send the user to /login"
// without each call site having to re-implement that branch. Cookie auth means
// we don't have to set headers — the browser already attaches the session
// cookie to same-origin requests.
async function authedFetch(input, init = {}) {
  const res = await fetch(input, init);
  if (res.status === 401) {
    // Best-effort; ignore failures (we're navigating away anyway).
    window.location.href = "/login";
    // Throwing here aborts the caller's success path. Caller's catch logs
    // a toast that the user won't see (we've already navigated).
    throw new Error("not authenticated");
  }
  return res;
}

async function logout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch {
    // Ignore network errors — the cookie's already going to be cleared
    // server-side or client-side eventually.
  }
  window.location.href = "/login";
}

// ---------- toasts ----------
function toast(message, type = "info", ms = 4000, opts = {}) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  // textContent for the message keeps the no-XSS guarantee; the action
  // button is built via DOM API so its label gets the same treatment.
  const msgSpan = document.createElement("span");
  msgSpan.className = "toast-message";
  msgSpan.textContent = message;
  el.appendChild(msgSpan);
  if (opts.action && typeof opts.action.handler === "function" && opts.action.label) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-action";
    btn.textContent = opts.action.label;
    btn.addEventListener("click", () => {
      try { opts.action.handler(); } catch (e) { console.error("toast action failed", e); }
      // Dismiss immediately on action click — user got the feedback they
      // wanted, no reason to keep the banner up.
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 200);
    });
    el.appendChild(btn);
  }
  els.toastContainer.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.2s";
    setTimeout(() => el.remove(), 250);
  }, ms);
}

// ---------- agent profile ----------
const PROFILE_KEY = "agent.profile.v1";

function loadProfile() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(PROFILE_KEY) || "{}"); } catch {}
  document.querySelectorAll("[data-profile]").forEach((input) => {
    const key = input.dataset.profile;
    if (saved[key] !== undefined) input.value = saved[key];
  });
}

function readProfileFromInputs() {
  const profile = {};
  document.querySelectorAll("[data-profile]").forEach((input) => {
    profile[input.dataset.profile] = input.value.trim() || null;
  });
  return profile;
}

function saveProfile() {
  const profile = readProfileFromInputs();
  localStorage.setItem(PROFILE_KEY, JSON.stringify(profile));
  toast("Agent profile saved", "success", 2000);
}

// ---------- attachments ----------
function renderAttachments() {
  els.attachments.innerHTML = "";
  if (attachedImages.length === 0) {
    els.attachments.hidden = true;
    return;
  }
  els.attachments.hidden = false;
  attachedImages.forEach((file, idx) => {
    const wrap = document.createElement("div");
    wrap.className = "attachment";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(file);
    img.onload = () => URL.revokeObjectURL(img.src);
    wrap.appendChild(img);
    const x = document.createElement("button");
    x.type = "button";
    x.className = "attachment-remove";
    x.textContent = "×";
    x.title = `Remove ${file.name || "image"}`;
    x.addEventListener("click", () => {
      attachedImages.splice(idx, 1);
      renderAttachments();
    });
    wrap.appendChild(x);
    els.attachments.appendChild(wrap);
  });
}

function addImages(files) {
  for (const f of files) {
    if (f && f.type && f.type.startsWith("image/")) attachedImages.push(f);
  }
  renderAttachments();
}

// ---------- field path read/write ----------
function setByPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null || typeof cur[parts[i]] !== "object") cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

function getByPath(obj, path) {
  let cur = obj;
  for (const part of path.split(".")) {
    if (cur == null) return undefined;
    cur = cur[part];
  }
  return cur;
}

function populateFields(data) {
  document.querySelectorAll("[data-path]").forEach((input) => {
    const value = getByPath(data, input.dataset.path);
    let str = "";
    if (Array.isArray(value)) {
      str = value.join(value.length === 2 ? " & " : ", ");
    } else if (value != null) {
      str = String(value);
    }
    input.value = str;
    if (str) input.classList.add("from-extract");
    else input.classList.remove("from-extract");
  });
  // Keep the chip strip in lockstep with the accordion. Live-stream events
  // (workstream A) populate chips one-by-one as they arrive; this is the
  // catch-up for fields that have a value in the payload but no chip event
  // landed for them (smaller AI responses, defaults, etc.).
  reconcileFromPayload(data);
}

function collectFields() {
  const fields = {};
  document.querySelectorAll("[data-path]").forEach((input) => {
    const path = input.dataset.path;
    const raw = input.value.trim();
    let value = raw === "" ? null : raw;
    if (input.dataset.list === "true" && value) {
      value = raw
        .split(/\s*(?:&|,| and )\s*/i)
        .map((s) => s.trim())
        .filter(Boolean);
    }
    setByPath(fields, path, value);
  });
  for (const key of ["tenant_or_buyer_names", "seller_names"]) {
    if (!Array.isArray(fields[key])) {
      fields[key] = fields[key] ? [fields[key]] : [];
    }
  }
  if (!fields.property) fields.property = {};
  return fields;
}

// ---------- dynamic template list ----------

async function fetchTemplates() {
  const res = await authedFetch("/api/templates");
  if (!res.ok) throw new Error(`templates list failed (${res.status})`);
  const data = await res.json();
  return data.templates || [];
}

function renderEmptyState() {
  // First-run UX: agent just signed up, has no templates yet. The big
  // empty state is the primary onboarding surface — pointing them at the
  // "Upload template" button below shouldn't be a treasure hunt.
  els.docList.innerHTML = `
    <div class="doc-list-empty">
      <div class="doc-list-empty-icon" aria-hidden="true">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="9" y1="15" x2="15" y2="15"/>
          <line x1="12" y1="12" x2="12" y2="18"/>
        </svg>
      </div>
      <h3 class="doc-list-empty-title">No templates yet</h3>
      <p class="doc-list-empty-sub">Upload a PDF with fillable form fields to get started. The AI will map each field to your transaction data.</p>
      <button type="button" class="btn-primary btn-sm" id="empty-upload-btn">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        Upload your first template
      </button>
    </div>
  `;
  // Wire the CTA to the existing upload-modal opener.
  const cta = document.getElementById("empty-upload-btn");
  if (cta) cta.addEventListener("click", openUploadModal);
}

function renderDocCards(templates, opts = {}) {
  // Preserve existing checkbox state across re-renders so a user-toggled
  // doc doesn't snap back to checked when we refetch the list.
  const previousChecked = new Map();
  for (const card of els.docList.querySelectorAll(".doc-card")) {
    const key = card.dataset.doc;
    const cb = card.querySelector('input[type="checkbox"]');
    if (key && cb) previousChecked.set(key, cb.checked);
  }

  els.docList.innerHTML = "";
  IMPLEMENTED_DOCS.clear();

  if (!templates.length) {
    renderEmptyState();
    updateGenerateBar();
    return;
  }

  for (const t of templates) {
    IMPLEMENTED_DOCS.add(t.id);
    if (t.title) FRIENDLY[t.id] = t.title;

    const card = document.createElement("article");
    card.className = "doc-card";
    card.dataset.doc = t.id;

    // New uploads auto-check; existing cards preserve their previous state.
    // First load defaults to checked so the user doesn't have to click every
    // template before extracting.
    const checked = previousChecked.has(t.id)
      ? previousChecked.get(t.id)
      : (opts.autoCheck === t.id || !previousChecked.size);

    // checkbox
    const checkLabel = document.createElement("label");
    checkLabel.className = "doc-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.addEventListener("change", updateGenerateBar);
    const checkBox = document.createElement("span");
    checkBox.className = "check-box";
    checkLabel.appendChild(cb);
    checkLabel.appendChild(checkBox);
    card.appendChild(checkLabel);

    const body = document.createElement("div");
    body.className = "doc-body";

    const row = document.createElement("div");
    row.className = "doc-row";
    const title = document.createElement("h3");
    title.className = "doc-title";
    title.textContent = t.title;
    row.appendChild(title);

    const actions = document.createElement("div");
    actions.className = "doc-card-actions";
    const pill = document.createElement("span");
    pill.className = "readiness readiness-pending";
    pill.textContent = "awaiting extraction";
    actions.appendChild(pill);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn-icon-sm";
    del.title = `Delete ${t.title}`;
    del.setAttribute("aria-label", `Delete ${t.title}`);
    del.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
    del.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      deleteTemplate(t);
    });
    actions.appendChild(del);
    row.appendChild(actions);
    body.appendChild(row);

    const sub = document.createElement("p");
    sub.className = "doc-sub";
    if (t.extra_field_count > 0) {
      sub.textContent = `${t.extra_field_count} extra field${t.extra_field_count === 1 ? "" : "s"} the AI will look for in your notes.`;
    } else {
      sub.textContent = "Fields map to the canonical schema.";
    }
    body.appendChild(sub);

    const meta = document.createElement("div");
    meta.className = "doc-meta";
    if (t.status === "ready") {
      const p = document.createElement("span");
      p.className = "meta-pill ready";
      p.textContent = "ready";
      meta.appendChild(p);
    } else if (t.status === "pending_review") {
      const p = document.createElement("span");
      p.className = "meta-pill warn";
      p.textContent = "needs review";
      meta.appendChild(p);
    }
    body.appendChild(meta);

    card.appendChild(body);
    els.docList.appendChild(card);
  }

  computeReadinessAfterExtract();
  updateGenerateBar();
}

async function deleteTemplate(template) {
  const ok = window.confirm(`Delete "${template.title}"? This can't be undone.`);
  if (!ok) return;
  try {
    const res = await authedFetch(`/api/templates/${encodeURIComponent(template.id)}`, {
      method: "DELETE",
    });
    if (!res.ok && res.status !== 204) {
      const detail = await res.text();
      throw new Error(`delete failed (${res.status}): ${detail}`);
    }
    knownTemplates = knownTemplates.filter((t) => t.id !== template.id);
    renderDocCards(knownTemplates);
    toast(`Deleted ${template.title}`, "success", 2500);
  } catch (e) {
    toast(e.message || "delete failed", "error");
  }
}

async function loadTemplates(opts = {}) {
  els.docListLoading.hidden = false;
  try {
    knownTemplates = await fetchTemplates();
    els.docListLoading.hidden = true;
    renderDocCards(knownTemplates, opts);
  } catch (e) {
    els.docListLoading.hidden = true;
    toast(e.message || "couldn't load templates", "error");
    els.docList.innerHTML = '<div class="hint" style="padding: 16px;">Couldn\'t load templates. Refresh to retry.</div>';
  }
}

// ---------- doc card readiness (selection mode) ----------
function setReadiness(card, level, text) {
  const pill = card.querySelector(".readiness");
  if (!pill) return;
  pill.className = `readiness readiness-${level}`;
  pill.textContent = text;
}

// Per-group fill counter. Each <details class="field-group"> gets a
// ".field-group-count" badge showing "3 of 5" — filled / total inputs in
// that group. Color shifts: subtle when empty, amber when partial, green
// when full. Runs on every input event so it stays live as the user types.
function updateFieldGroupCounts() {
  const groups = document.querySelectorAll("details.field-group");
  groups.forEach((g) => {
    const inputs = g.querySelectorAll("input, select, textarea");
    if (inputs.length === 0) return;
    let filled = 0;
    inputs.forEach((el) => {
      const v = (el.value || "").trim();
      if (v) filled++;
    });
    const badge = g.querySelector(".field-group-count");
    if (!badge) return;
    badge.textContent = `${filled} of ${inputs.length}`;
    if (filled === 0) {
      badge.dataset.state = "empty";
    } else if (filled === inputs.length) {
      badge.dataset.state = "full";
    } else {
      badge.dataset.state = "partial";
    }
  });
}

// Which fields make sense for each transaction kind. The "N missing" badge
// filters against this so a lease deal doesn't get yelled at for not having
// a purchase_price, and a sale deal doesn't get yelled at for missing rent.
// "both" means the field applies regardless of transaction type. Anything not
// listed here defaults to "both" — better to over-flag than to silently skip
// a field the user really did need.
const FIELD_APPLIES_TO = {
  // Lease-only
  "lease_start": "lease",
  "lease_end": "lease",
  "monthly_rent": "lease",
  // Sale-only
  "purchase_price": "sale",
  "earnest_money": "sale",
  "closing_date": "sale",
  "seller_names": "sale",
};

function computeReadinessAfterExtract() {
  const fields = collectFields();
  const profile = readProfileFromInputs();
  const have = (path) => {
    const v = getByPath({ ...fields, agent: profile }, path);
    return Array.isArray(v) ? v.length > 0 : (v != null && String(v).trim() !== "");
  };

  // The user's transaction type drives field filtering. If they haven't
  // declared one yet (or the AI couldn't infer it), we keep all fields in
  // play — an honest "N missing" is better than silently hiding things.
  const txType = (fields.transaction_type || "").toString().toLowerCase();
  const txKnown = txType === "lease" || txType === "sale";
  const fieldApplies = (path) => {
    if (!txKnown) return true;
    const applies = FIELD_APPLIES_TO[path] || "both";
    return applies === "both" || applies === txType;
  };

  // Required fields per known IL default. Custom templates are checked
  // generically — we show "ready" once extraction has run because the
  // template's extras were filled (or set to null when the notes didn't
  // mention them, which is fine for fill).
  const checks = {
    lease_invoice:  ["property.address", "property.city", "lease_start", "tenant_or_buyer_names", "commission_amount", "agent.name"],
    lease_abstract: ["property.address", "property.city", "lease_start", "lease_end", "monthly_rent", "commission_amount", "agent.name"],
    tenant_rep:     ["property.address", "property.city", "tenant_or_buyer_names", "commission_amount", "agent.name"],
    multiboard:     ["property.address", "property.city", "tenant_or_buyer_names", "seller_names", "purchase_price", "earnest_money", "closing_date", "agent.name"],
  };

  // Whole-template applicability. If a doc is fundamentally lease-only and
  // the user is doing a sale, we want to say "sale only" rather than
  // "8 missing" — because every required field for that doc is irrelevant
  // and the count would be a misleading scare number.
  const TEMPLATE_APPLIES_TO = {
    lease_invoice:  "lease",
    lease_abstract: "lease",
    tenant_rep:     "lease",
    multiboard:     "sale",
  };

  document.querySelectorAll(".doc-card").forEach((card) => {
    // Until the user clicks Extract, the form on the left is empty by
    // design — flagging "N missing" against an empty form just looks
    // accusatory. Show neutral "awaiting extraction" instead, then
    // switch to per-field accounting once we have something to check.
    if (!extracted) {
      setReadiness(card, "pending", "awaiting extraction");
      return;
    }
    const key = card.dataset.doc;

    // Whole-template applicability: if we know the transaction type and the
    // doc is for the other kind, surface that as the badge rather than a
    // missing-fields count. Users can still check the box and generate; the
    // badge just tells them why most of the fields look empty.
    const tApplies = TEMPLATE_APPLIES_TO[key];
    if (txKnown && tApplies && tApplies !== txType) {
      setReadiness(card, "pending", `${tApplies} only`);
      return;
    }

    const required = checks[key];
    if (required) {
      // Filter to fields that apply to the current transaction type before
      // counting. A lease deal shouldn't see "8 missing" on a doc whose
      // shortfalls are all sale-only fields — those aren't actually missing,
      // they're not applicable.
      const applicable = required.filter(fieldApplies);
      const missing = applicable.filter((p) => !have(p));
      if (applicable.length === 0) {
        // Edge case: every required field was filtered out. Treat as ready —
        // the doc has nothing to fill from the canonical schema and will
        // rely on its own extras (or the user's manual edits) at generate time.
        setReadiness(card, "ready", "ready");
      } else if (missing.length === 0) {
        setReadiness(card, "ready", "ready");
      } else if (missing.length === applicable.length) {
        setReadiness(card, "missing", `${missing.length} missing`);
      } else {
        setReadiness(card, "partial", `${missing.length} missing`);
      }
    } else {
      // Custom template — no per-field required-list. Once extraction
      // has run, the AI either populated template_extras.<id> or didn't;
      // either way the user can review on the form and generate.
      setReadiness(card, "ready", "ready");
    }
  });
}

function updateGenerateBar() {
  const cards = document.querySelectorAll(".doc-card");
  let selected = 0;
  let ready = 0;
  cards.forEach((card) => {
    const checked = card.querySelector('input[type="checkbox"]').checked;
    card.classList.toggle("unchecked", !checked);
    if (!checked) return;
    selected++;
    const pill = card.querySelector(".readiness");
    if (pill?.classList.contains("readiness-ready")) ready++;
  });

  const state = computeGenerateState();
  const docWord = selected === 1 ? "document" : "documents";
  const btnLabel = els.generateBtn.querySelector(".btn-label");

  // Set data-state on the button so CSS can swap fill/outline/disabled tone.
  // The label inside is purely textual; the visual weight comes from the state.
  els.generateBtn.dataset.state = state;
  els.generateBtn.classList.toggle("btn-view", state === "view");

  if (btnLabel) {
    if (!extracted) {
      btnLabel.textContent = "Extract first to generate";
    } else if (selected === 0) {
      btnLabel.textContent = "Pick a document";
    } else if (state === "view") {
      btnLabel.textContent = `View ${selected} ${docWord}`;
    } else if (lastGenerateSnapshot) {
      // We've generated before; current inputs differ → this is a re-fill.
      btnLabel.textContent = `Regenerate ${selected} ${docWord}`;
    } else {
      btnLabel.textContent = `Generate ${selected} ${docWord}`;
    }
  }

  if (state === "view") {
    els.generateSummary.innerHTML = `<strong>Documents ready.</strong> View, edit, or change inputs to regenerate.`;
  } else if (extracted) {
    els.generateSummary.innerHTML = ready === selected
      ? `<strong>All ${selected} ready.</strong> Review fields, then generate.`
      : `<strong>${ready} of ${selected} ready.</strong> Fill remaining fields above.`;
  } else {
    els.generateSummary.innerHTML = selected === 0
      ? "Pick at least one document above."
      : `<strong>${selected} ${docWord}</strong> picked. Add notes, then extract.`;
  }

  // The button stays clickable in 'view' state — clicking re-enters preview.
  // Only disabled when there's literally nothing to do.
  els.generateBtn.disabled = state === "idle";
}

// ---------- loading state ----------
function setLoading(button, isLoading) {
  const label = button.querySelector(".btn-label");
  const spinner = button.querySelector(".btn-spinner");
  if (label) label.hidden = isLoading;
  if (spinner) spinner.hidden = !isLoading;
  button.disabled = isLoading;
}

// ---------- PDF preview (image + overlay inputs) ----------
// Each page is rendered server-side to a PNG (pypdfium2). The frontend stacks
// the page images and absolutely-positions <input>/<textarea>/<input type=checkbox>
// over each AcroForm field rect. Edits flow into pendingEdits; a Save button
// posts them to /api/edit which re-fills the PDF and returns a new render.

// Per-doc preview state. docKey -> {base64, preview, pendingEdits, dirty, pages}
const docState = new Map();

// Maximum CSS width we render the PDF page at (matches the existing canvas cap).
const PREVIEW_MAX_WIDTH_CSS = 820;

async function fetchPreview(base64Pdf) {
  const res = await authedFetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base64_pdf: base64Pdf }),
  });
  if (!res.ok) throw new Error(`preview failed (${res.status})`);
  return res.json();
}

function ensureDocState(doc) {
  let s = docState.get(doc.document);
  if (!s) {
    s = {
      base64: doc.base64,
      filename: doc.filename,
      preview: null,
      pendingEdits: new Map(),
      dirty: false,
      pages: null,        // cached DOM nodes
    };
    docState.set(doc.document, s);
  }
  return s;
}

function renderFieldOverlay(field, scale, state) {
  const [x, y, w, h] = field.rect_px;
  const cssH = h * scale;
  const node = document.createElement("div");
  node.className = "pdf-field";
  node.style.left = `${x * scale}px`;
  node.style.top = `${y * scale}px`;
  node.style.width = `${w * scale}px`;
  node.style.height = `${cssH}px`;
  // Match input font size to the rendered field height. PDF form blanks are
  // typically ~14pt tall at 100% scale; we leave 30% headroom so text doesn't
  // clip vertically.
  node.style.setProperty("--field-font-size", `${Math.max(9, Math.min(16, cssH * 0.7))}px`);

  const onChange = (newValue) => {
    if (newValue === field.value) {
      // User reverted to original; drop from pending edits.
      state.pendingEdits.delete(field.name);
    } else {
      state.pendingEdits.set(field.name, newValue);
    }
    state.dirty = state.pendingEdits.size > 0;
    // Inline PDF edits diverge from the canonical-fields panel, which means a
    // regenerate would rebuild from the panel and silently overwrite them.
    // Flag dirty so the bar flips to "Regenerate" and runGenerate prompts a
    // confirm before firing. Cleared by saveEditsForActiveDoc on success and
    // by a successful regenerate.
    previewEditsDirty = true;
    syncSaveBar(state);
    updateGenerateBar();
  };

  if (field.field_type === "/Btn") {
    // Real checkbox = states are exactly some subset of {/Off, /On}. Radio
    // groups have additional state names like /Choice1, /Seller's Brokerage,
    // etc. — toggling those as a checkbox would clobber the real selection
    // because we'd write /On (which isn't a valid state for that field) and
    // pdf_fill would set every kid's /AS to /Off. Render those as read-only
    // until we have proper radio UI in Pillar 2.
    const states = field.states || [];
    const isRadio = states.some((s) => s !== "/Off" && s !== "/On");
    if (isRadio) {
      node.classList.add("pdf-field-readonly");
      node.title = "Radio group — edit on the form to the left";
    } else {
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = field.value && field.value !== "/Off" && field.value !== "";
      cb.addEventListener("change", () => {
        onChange(cb.checked ? "/On" : "/Off");
      });
      node.classList.add("pdf-field-btn");
      node.appendChild(cb);
    }
  } else if (field.field_type === "/Ch") {
    // Dropdowns/listboxes: backend doesn't currently expose /Opt (the choices)
    // so we can't render a real <select>. Plain text input would let users
    // type values that don't match any option, which appear blank in Acrobat.
    // Read-only marker keeps the form trustworthy.
    node.classList.add("pdf-field-readonly");
    node.title = "Dropdown — edit on the form to the left";
  } else if (field.field_type === "/Sig") {
    // Signatures aren't editable inline. Show a passive marker so the user
    // sees where to sign in the downloaded PDF.
    node.classList.add("pdf-field-sig");
    node.title = "Signature field — sign in your PDF reader";
  } else {
    // Text. Most form blanks are single-line; only treat very tall rects (4+
    // text lines) as multi-line. h is in PNG pixels at RENDER_SCALE=2, so a
    // ~14pt line is ~28px; "more than 4 lines" means h > ~120px.
    const isTall = h > 120;
    const el = document.createElement(isTall ? "textarea" : "input");
    if (!isTall) el.type = "text";
    el.value = field.value || "";
    el.spellcheck = false;
    el.addEventListener("input", () => onChange(el.value));
    node.appendChild(el);
  }
  node.dataset.fieldName = field.name;
  return node;
}

function buildPagesForState(state) {
  const fragment = document.createDocumentFragment();
  // Group fields by page once.
  const fieldsByPage = new Map();
  for (const f of state.preview.fields) {
    if (!fieldsByPage.has(f.page)) fieldsByPage.set(f.page, []);
    fieldsByPage.get(f.page).push(f);
  }

  const pageNodes = [];
  for (const p of state.preview.pages) {
    const wrapper = document.createElement("div");
    wrapper.className = "pdf-page";
    const cssWidth = Math.min(PREVIEW_MAX_WIDTH_CSS, p.width_px);
    const scale = cssWidth / p.width_px;
    wrapper.style.width = `${cssWidth}px`;
    wrapper.style.height = `${p.height_px * scale}px`;

    const img = document.createElement("img");
    img.className = "pdf-page-img";
    img.alt = `Page ${p.page}`;
    img.src = `data:image/png;base64,${p.image_b64}`;
    wrapper.appendChild(img);

    const overlay = document.createElement("div");
    overlay.className = "pdf-field-layer";
    for (const f of fieldsByPage.get(p.page) || []) {
      overlay.appendChild(renderFieldOverlay(f, scale, state));
    }
    wrapper.appendChild(overlay);

    pageNodes.push(wrapper);
    fragment.appendChild(wrapper);
  }
  state.pages = pageNodes;
  return fragment;
}

async function renderDocument(doc) {
  const state = ensureDocState(doc);
  if (state.pages) return state.pages;
  if (!state.preview) {
    state.preview = await fetchPreview(state.base64);
  }
  buildPagesForState(state);
  return state.pages;
}

function syncSaveBar(state) {
  // Only show the Save button for the currently active doc.
  const activeState = activeDocKey ? docState.get(activeDocKey) : null;
  if (state !== activeState) return;
  if (!els.saveEditsBtn) return;
  els.saveEditsBtn.hidden = !state.dirty;
  if (state.dirty && els.saveEditsLabel) {
    const n = state.pendingEdits.size;
    els.saveEditsLabel.textContent = `Save ${n} edit${n === 1 ? "" : "s"}`;
  }
}

async function saveEditsForActiveDoc() {
  if (!activeDocKey) return;
  const state = docState.get(activeDocKey);
  if (!state || !state.dirty) return;

  const edits = {};
  for (const [k, v] of state.pendingEdits) edits[k] = v;

  // Disable the overlay inputs while the save is in flight. Otherwise a user
  // who keeps typing after clicking Save loses those characters when we
  // pendingEdits.clear() and rebuild from the server's response.
  const liveInputs = els.pagesScroll.querySelectorAll(".pdf-field input, .pdf-field textarea");
  liveInputs.forEach((el) => { el.disabled = true; });

  setLoading(els.saveEditsBtn, true);
  try {
    const res = await authedFetch("/api/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base64_pdf: state.base64, edits }),
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`save failed (${res.status}): ${detail}`);
    }
    const data = await res.json();
    state.base64 = data.document.base64;
    state.preview = data.preview;
    state.pendingEdits.clear();
    state.dirty = false;
    state.pages = null;

    // Inline edits are now baked into the new PDF bytes — the snapshot
    // doesn't need to invalidate. Clear the dirty flag so the bar can return
    // to 'view' state. (If other docs in the batch still have unsaved inline
    // edits, this gets re-set on their next keystroke.)
    previewEditsDirty = false;
    updateGenerateBar();

    // Also update lastGenerated so the tab download button gets the new bytes.
    const inList = lastGenerated.find((d) => d.document === activeDocKey);
    if (inList) inList.base64 = state.base64;

    // Re-render the active tab from the fresh preview.
    clearPagesContainer();
    buildPagesForState(state);
    state.pages.forEach((p) => els.pagesScroll.appendChild(p));
    syncSaveBar(state);
    toast("Edits saved", "success", 2000);
  } catch (e) {
    console.error(e);
    toast(e.message || "save failed", "error");
    // Re-enable the existing inputs so the user can retry. On the success
    // path these get replaced with fresh DOM, so this only matters on errors.
    liveInputs.forEach((el) => { el.disabled = false; });
  } finally {
    setLoading(els.saveEditsBtn, false);
  }
}

function downloadDoc(doc) {
  const a = document.createElement("a");
  a.href = `data:${doc.content_type};base64,${doc.base64}`;
  a.download = doc.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

const DOWNLOAD_ICON_SVG = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`;

function renderTabs(docs) {
  els.tabStrip.innerHTML = "";
  docs.forEach((doc) => {
    const tab = document.createElement("button");
    tab.className = "tab";
    tab.dataset.doc = doc.document;
    tab.setAttribute("role", "tab");

    const title = document.createElement("span");
    title.textContent = FRIENDLY[doc.document] || doc.document;
    tab.appendChild(title);

    const dl = document.createElement("span");
    dl.className = "tab-download";
    dl.title = `Download ${doc.filename}`;
    dl.innerHTML = DOWNLOAD_ICON_SVG;
    dl.addEventListener("click", (e) => {
      e.stopPropagation();
      downloadDoc(doc);
    });
    tab.appendChild(dl);

    tab.addEventListener("click", () => setActiveTab(doc.document));
    els.tabStrip.appendChild(tab);
  });
}

function clearPagesContainer() {
  Array.from(els.pagesScroll.children).forEach((c) => {
    if (c !== els.pagesLoading) c.remove();
  });
}

function renderUncertainFields(doc) {
  // Collapsed banner above the rendered PDF listing fields the AI wasn't
  // sure about. Default collapsed so the PDF stays visible without scrolling
  // past 96 rows of warnings (real Multi-Board case). Native <details> so
  // the toggle is keyboard-accessible and works without extra JS. The host
  // <div id="uncertain-fields"> is itself the <details> element — no extra
  // wrapper. State persists across regenerates: if the user expanded the
  // panel and then regenerates the same template, the new banner stays open.
  if (!els.uncertainFields) return;
  const uncertain = (doc && doc.uncertain_fields) || [];
  if (!uncertain.length) {
    els.uncertainFields.hidden = true;
    els.uncertainFields.innerHTML = "";
    els.uncertainFields.removeAttribute("open");
    return;
  }
  const wasOpen = els.uncertainFields.hasAttribute("open");
  els.uncertainFields.hidden = false;
  const count = uncertain.length;
  const heading = `We weren't sure about ${count} field${count === 1 ? "" : "s"}`;
  // Pre-shape each row into a label + readable description. The "proposed"
  // string is internal (canonical_path or extra_field_name) — show it as
  // a hint, not the headline.
  const rows = uncertain.map((u) => {
    const label = u.pdf_field || "unnamed field";
    const hint = u.kind === "canonical"
      ? `Looked like: ${u.proposed.replace(/_/g, " ")}`
      : `Looked like custom field: ${u.proposed.replace(/_/g, " ")}`;
    return `
      <li class="uncertain-row">
        <span class="uncertain-label">${escapeHtml(label)}</span>
        <span class="uncertain-hint">${escapeHtml(hint)}</span>
      </li>
    `;
  }).join("");
  els.uncertainFields.innerHTML = `
    <summary class="uncertain-header">
      <svg class="uncertain-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 9v4M12 17h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      </svg>
      <strong>${escapeHtml(heading)}</strong>
      <span class="uncertain-sub">left blank in the PDF, fill in by hand</span>
      <svg class="uncertain-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="6 9 12 15 18 9"/>
      </svg>
    </summary>
    <ul class="uncertain-list">${rows}</ul>
  `;
  if (wasOpen) els.uncertainFields.setAttribute("open", "");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}

async function setActiveTab(docKey) {
  if (activeDocKey === docKey) return;
  activeDocKey = docKey;

  els.tabStrip.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.doc === docKey);
  });

  const doc = lastGenerated.find((d) => d.document === docKey);
  if (!doc) return;

  // Surface uncertain fields BEFORE the pages render so the user sees it
  // immediately when they switch tabs.
  renderUncertainFields(doc);

  clearPagesContainer();
  // If we already have a built page list for this doc we render instantly;
  // otherwise show the loading spinner while /api/preview comes back.
  const cached = docState.get(docKey)?.pages != null;
  els.pagesLoading.hidden = cached;

  try {
    const pages = await renderDocument(doc);
    if (activeDocKey !== docKey) return; // user switched away mid-render
    pages.forEach((p) => els.pagesScroll.appendChild(p));
    els.pagesScroll.scrollTop = 0;
    const state = docState.get(docKey);
    if (state) syncSaveBar(state);
  } catch (e) {
    console.error("preview render failed:", e);
    toast(`Preview render failed for ${FRIENDLY[docKey] || docKey}`, "error");
  } finally {
    if (activeDocKey === docKey) els.pagesLoading.hidden = true;
  }
}

function enterPreviewMode(docs) {
  lastGenerated = docs;

  const stillPresent = activeDocKey && docs.some((d) => d.document === activeDocKey);
  const next = stillPresent ? activeDocKey : docs[0].document;
  activeDocKey = null; // force setActiveTab to render

  renderTabs(docs);
  els.selectionMode.hidden = true;
  els.previewMode.hidden = false;
  if (els.saveEditsBtn) els.saveEditsBtn.hidden = true;

  setActiveTab(next);

  // Pre-fetch previews for the other tabs in the background so tab switching
  // is instant (no spinner).
  for (const d of docs) {
    const state = ensureDocState(d);
    if (d.document !== next && !state.preview) {
      fetchPreview(state.base64).then((p) => { state.preview = p; }).catch((e) => {
        console.error("background preview fetch failed", d.document, e);
      });
    }
  }
}

function exitPreviewMode() {
  els.previewMode.hidden = true;
  if (els.uncertainFields) {
    els.uncertainFields.hidden = true;
    els.uncertainFields.innerHTML = "";
    els.uncertainFields.removeAttribute("open");
  }
  els.selectionMode.hidden = false;
}

// ---------- API calls ----------

// Returns the doc keys currently checked in the selection pane. Used by
// both runExtract (so /api/extract can include those templates' extras in
// the dynamic schema) and runGenerate (which docs to fill).
function checkedDocKeys() {
  const keys = [];
  document.querySelectorAll(".doc-card").forEach((card) => {
    const checked = card.querySelector('input[type="checkbox"]').checked;
    if (checked && card.dataset.doc) keys.push(card.dataset.doc);
  });
  return keys;
}

// Tip messages cycled every 4s during extraction. gpt-5 takes 15-45s; the
// rotating tips signal "still alive, doing work" without us actually knowing
// the model's progress.
const EXTRACT_TIPS = [
  "Reading your notes…",
  "Identifying the property…",
  "Looking for dates and money amounts…",
  "Cross-referencing with attached images…",
  "Almost there — finalizing structured fields…",
];

function startExtractProgress() {
  els.extractProgress.hidden = false;
  els.extractProgressElapsed.textContent = "0s";
  els.extractProgressTip.textContent = EXTRACT_TIPS[0];
  const startedAt = performance.now();
  let tipIdx = 0;
  const elapsedTimer = setInterval(() => {
    const s = Math.floor((performance.now() - startedAt) / 1000);
    els.extractProgressElapsed.textContent = `${s}s`;
  }, 200);
  const tipTimer = setInterval(() => {
    tipIdx = (tipIdx + 1) % EXTRACT_TIPS.length;
    els.extractProgressTip.textContent = EXTRACT_TIPS[tipIdx];
  }, 4000);
  return () => {
    clearInterval(elapsedTimer);
    clearInterval(tipTimer);
    els.extractProgress.hidden = true;
  };
}

// ---------- live extraction (workstream A) ----------
//
// Debounced, diff-aware live extraction. Strategy from plan-eng-review issue 1.1:
//   - cheap "live" tier (gpt-5-mini), never carries images
//   - skip if notes haven't meaningfully changed since last call
//   - skip if a full extraction is in flight (extractBtn busy)
//   - one SSE stream per call; previous in-flight stream is aborted
//
// State outside of the debounce timer so the diff check survives multiple
// keypresses without flapping.
let _lastLiveExtractedNotes = "";
let _liveDebounceTimer = null;
let _liveStreamController = null;
const LIVE_DEBOUNCE_MS = 1200;
const LIVE_MIN_DIFF_CHARS = 30;

function scheduleLiveExtract() {
  if (_liveDebounceTimer) clearTimeout(_liveDebounceTimer);
  _liveDebounceTimer = setTimeout(runLiveExtract, LIVE_DEBOUNCE_MS);
}

async function runLiveExtract() {
  // Don't run during a full extraction — it would race with the bigger,
  // truthier response and reorder chips.
  if (els.extractBtn?.classList.contains("is-loading")) return;

  const notes = els.notes.value || "";
  if (!notes.trim()) return;

  // Diff check: skip if the user only added trivial whitespace or fewer
  // chars than LIVE_MIN_DIFF_CHARS since the last successful live call.
  const diff = Math.abs(notes.length - _lastLiveExtractedNotes.length);
  if (diff < LIVE_MIN_DIFF_CHARS && _lastLiveExtractedNotes !== "") return;

  // Cancel any in-flight stream so we don't get out-of-order chip events.
  if (_liveStreamController) {
    try { _liveStreamController.abort(); } catch (_) {}
    _liveStreamController = null;
  }

  const controller = new AbortController();
  _liveStreamController = controller;

  const formData = new FormData();
  formData.append("notes", notes);
  formData.append("tier", "live");
  formData.append("active_template_ids", checkedDocKeys().join(","));

  try {
    const res = await fetch("/api/extract/stream", {
      method: "POST",
      body: formData,
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      // Live tier failed quietly — the user still has the full Extract button.
      return;
    }
    // Parse SSE: split on blank-line, dispatch by event:/data: lines.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        handleSseBlock(block);
      }
    }
    _lastLiveExtractedNotes = notes;
  } catch (e) {
    if (e.name !== "AbortError") {
      console.warn("live extract failed", e);
    }
  } finally {
    if (_liveStreamController === controller) _liveStreamController = null;
  }
}

function handleSseBlock(block) {
  let eventName = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (eventName === "chip") {
    try {
      applyChipEvent(JSON.parse(data));
    } catch (e) { console.warn("bad chip event", e); }
  } else if (eventName === "done") {
    try {
      const payload = JSON.parse(data);
      // Lazily populate the accordion too — the user hasn't clicked Extract
      // but if they open the parsed-fields panel after a live run, we want
      // it to reflect what the chips are showing.
      if (extracted && els.fields && !els.fields.hidden) {
        populateFields(payload);
      } else {
        reconcileFromPayload(payload);
      }
    } catch (e) { console.warn("bad done event", e); }
  } else if (eventName === "error") {
    console.warn("live extract error event", data);
  }
}

async function runExtract() {
  if (!els.notes.value.trim() && attachedImages.length === 0) {
    els.notes.focus();
    toast("Add notes or attach an image first", "error");
    return;
  }
  setLoading(els.extractBtn, true);
  const stopProgress = startExtractProgress();
  const formData = new FormData();
  formData.append("notes", els.notes.value);
  for (const file of attachedImages) {
    formData.append("images", file, file.name || "screenshot.png");
  }
  // Tell the backend which templates are active so any uploaded templates'
  // extra_fields join the dynamic extraction schema. Backend silently
  // ignores ids it doesn't recognize, so a stale list doesn't break extract.
  formData.append("active_template_ids", checkedDocKeys().join(","));
  try {
    const res = await authedFetch("/api/extract", { method: "POST", body: formData });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`extract failed (${res.status}): ${detail}`);
    }
    const data = await res.json();
    extracted = true;
    els.fields.hidden = false;
    populateFields(data);
    // Extract overwrites the panel from the fresh AI response — anything the
    // user had buffered is gone, so the pending flag should reset and the
    // Save button should disappear.
    fieldsPendingEdits = false;
    if (els.saveFieldsBtn) els.saveFieldsBtn.hidden = true;
    computeReadinessAfterExtract();
    updateFieldGroupCounts();
    updateGenerateBar();
    els.fields.scrollIntoView({ behavior: "smooth", block: "nearest" });
    toast("Fields extracted — review before generating", "success", 2500);
  } catch (e) {
    toast(e.message || "extract failed", "error");
    console.error(e);
  } finally {
    stopProgress();
    setLoading(els.extractBtn, false);
  }
}

async function runGenerate(triggerBtn) {
  // 'view' short-circuit: snapshot still matches the last successful generate
  // and there are no inline preview edits dirty → just re-enter preview mode
  // instead of paying the API call again. Only honored on the main bar button;
  // the in-preview Regenerate button always fires through.
  const fromMainBar = !triggerBtn || triggerBtn === els.generateBtn;
  if (fromMainBar && computeGenerateState() === "view" && lastGenerated.length > 0) {
    enterPreviewMode(lastGenerated);
    return;
  }

  // 2A: warn loudly if the user is about to overwrite inline preview edits.
  // We don't block — the canonical-fields panel is the source of truth — but
  // we make damn sure the user knows their on-PDF tweaks will disappear.
  if (previewEditsDirty) {
    const ok = window.confirm(
      "You have unsaved edits made directly on the PDF preview.\n\n" +
      "Regenerating will rebuild from the parsed fields panel and those inline " +
      "edits will be lost.\n\nContinue?"
    );
    if (!ok) return;
  }

  const allSelected = [];
  const skipped = [];
  for (const key of checkedDocKeys()) {
    if (IMPLEMENTED_DOCS.has(key)) allSelected.push(key);
    else skipped.push(key);
  }

  if (allSelected.length === 0) {
    toast("None of the selected documents are implemented yet", "error", 5000);
    return;
  }

  const btn = triggerBtn || els.generateBtn;
  setLoading(btn, true);
  // Capture the snapshot BEFORE the await — if the user edits while the request
  // is in flight, we want the post-success button state to reflect "you changed
  // things since you fired this", not "match".
  const snapshotAtRequest = currentSnapshot();
  try {
    const body = {
      fields: collectFields(),
      agent: readProfileFromInputs(),
      documents: allSelected,
    };
    const res = await authedFetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`generate failed (${res.status}): ${detail}`);
    }
    const data = await res.json();

    // Drop cached preview state for any docs we just regenerated so the new
    // /V values flow through cleanly.
    for (const doc of data.documents) {
      docState.delete(doc.document);
    }

    if (data.documents.length > 0) {
      enterPreviewMode(data.documents);
    }

    if (skipped.length > 0) {
      toast(`Skipped (not implemented yet): ${skipped.join(", ")}`, "info", 4500);
    }

    // Per-doc failures from /api/generate (e.g., one bad mapping in the batch).
    // Show each so the user knows which docs didn't make it.
    const failures = data.failures || [];
    if (failures.length > 0) {
      const summary = failures.map((f) => `${FRIENDLY[f.document] || f.document}: ${f.error}`).join("\n");
      toast(`${failures.length} doc${failures.length === 1 ? "" : "s"} failed:\n${summary}`, "error", 7000);
    }
    if (data.documents.length === 0 && failures.length > 0) {
      // Whole batch failed — make sure we don't leave the user staring at
      // a stale preview from the previous generate.
      exitPreviewMode();
    }

    // 1A: only update the snapshot if the generate fully succeeded. If any
    // doc failed, we leave the previous snapshot in place so the button stays
    // in 'regenerate' mode — clicking again will retry the failed docs rather
    // than silently flipping to 'view' on a partial outcome.
    if (failures.length === 0 && data.documents.length > 0) {
      lastGenerateSnapshot = snapshotAtRequest;
      previewEditsDirty = false;
      // Successful generate consumes any buffered field edits — the snapshot
      // now matches the live values, so the Save button's job is done.
      fieldsPendingEdits = false;
      if (els.saveFieldsBtn) els.saveFieldsBtn.hidden = true;
    }
    updateGenerateBar();
  } catch (e) {
    toast(e.message || "generate failed", "error");
    console.error(e);
  } finally {
    setLoading(btn, false);
  }
}

// ---------- wire up ----------
els.extractBtn.addEventListener("click", runExtract);
els.generateBtn.addEventListener("click", () => runGenerate(els.generateBtn));
els.regenerateBtn.addEventListener("click", () => runGenerate(els.regenerateBtn));
if (els.saveEditsBtn) {
  els.saveEditsBtn.addEventListener("click", saveEditsForActiveDoc);
}
els.backBtn.addEventListener("click", exitPreviewMode);

els.notes.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    runExtract();
  }
});

els.fields.addEventListener("input", () => {
  // Local UI (per-card readiness pills + per-group fill counts) stays live
  // because those reflect the in-progress state of the panel and don't claim
  // anything about the PDFs. The generate bar, however, is gated until the
  // user clicks "Save edits" — see fieldsPendingEdits.
  computeReadinessAfterExtract();
  updateFieldGroupCounts();
  if (!fieldsPendingEdits) {
    fieldsPendingEdits = true;
    if (els.saveFieldsBtn) els.saveFieldsBtn.hidden = false;
  }
});

function commitFieldEdits() {
  // Publishes buffered field edits to the generate bar. currentSnapshot()
  // already reads from live inputs, so the only state we change here is the
  // pending flag — clearing it lets updateGenerateBar() see the current
  // snapshot vs. lastGenerateSnapshot and flip View → Regenerate accordingly.
  fieldsPendingEdits = false;
  if (els.saveFieldsBtn) els.saveFieldsBtn.hidden = true;
  updateGenerateBar();
  toast("Edits saved — regenerate to update documents", "success", 2500);
}

if (els.saveFieldsBtn) {
  els.saveFieldsBtn.addEventListener("click", commitFieldEdits);
}

// (doc-card checkboxes are wired per-card in renderDocCards now)

// ---------- attachments wiring ----------
els.attachBtn.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", (e) => {
  addImages(e.target.files);
  e.target.value = "";
});

els.notes.addEventListener("paste", (e) => {
  const items = e.clipboardData?.items || [];
  const images = [];
  for (const item of items) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) images.push(file);
    }
  }
  if (images.length > 0) {
    e.preventDefault();
    addImages(images);
  }
});

["dragenter", "dragover"].forEach((evt) => {
  els.notesWrap.addEventListener(evt, (e) => {
    if (e.dataTransfer?.types?.includes("Files")) {
      e.preventDefault();
      els.notesWrap.classList.add("dragover");
    }
  });
});
["dragleave", "drop"].forEach((evt) => {
  els.notesWrap.addEventListener(evt, (e) => {
    if (evt === "dragleave" && e.target !== els.notesWrap) return;
    els.notesWrap.classList.remove("dragover");
  });
});
els.notesWrap.addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer?.files) addImages(e.dataTransfer.files);
});

// ---------- profile drawer ----------
els.profileBtn.addEventListener("click", () => {
  els.drawer.hidden = false;
});
els.profileSave.addEventListener("click", () => {
  saveProfile();
  els.drawer.hidden = true;
  // Profile values feed into /api/generate, so the snapshot must reflect them.
  // currentSnapshot() reads from the live inputs each call, but the bar's
  // displayed state is cached — recompute after save so changing the agent
  // name correctly flips View → Regenerate.
  updateGenerateBar();
});
els.drawer.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) els.drawer.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.drawer.hidden) els.drawer.hidden = true;
});

// ---------- upload-template modal (Pillar 2) ----------

// Stash the file the user picks so the Upload button can submit it. Form
// state lives here rather than scraping inputs at submit time so drag-drop
// and click-to-browse converge on the same code path.
let uploadFile = null;

function setUploadFile(file) {
  if (!file) {
    uploadFile = null;
    els.uploadDropPrimary.textContent = "Drop PDF here, or click to browse";
    els.uploadDrop.classList.remove("dragover");
  } else {
    uploadFile = file;
    els.uploadDropPrimary.textContent = file.name;
    // Auto-suggest a title from the filename if title is empty.
    if (!els.uploadTitle.value.trim()) {
      const stem = (file.name || "").replace(/\.pdf$/i, "").replace(/[_-]+/g, " ").trim();
      // Title-case-ish: capitalize first letter of each word.
      els.uploadTitle.value = stem.replace(/\b\w/g, (c) => c.toUpperCase());
    }
  }
  refreshUploadSubmit();
}

function refreshUploadSubmit() {
  const ready = !!uploadFile && els.uploadTitle.value.trim().length > 0;
  els.uploadSubmit.disabled = !ready;
}

function openUploadModal() {
  // Reset state every time so a previous attempt doesn't leak into the next.
  setUploadFile(null);
  els.uploadTitle.value = "";
  els.uploadFileInput.value = "";
  els.uploadForm.hidden = false;
  els.uploadProgress.hidden = true;
  setLoading(els.uploadSubmit, false);
  els.uploadCancel.disabled = false;
  for (const stage of els.uploadProgress.querySelectorAll(".stage")) {
    stage.dataset.state = stage.dataset.stage === "map" ? "active" : "";
  }
  els.stageExtractLabel.textContent = "Extracting form fields";
  els.uploadModal.hidden = false;
}

function closeUploadModal() {
  els.uploadModal.hidden = true;
}

async function submitUpload() {
  if (!uploadFile || !els.uploadTitle.value.trim()) return;
  // Swap to the staged-progress UI. Reading + Extracting flip to done as
  // soon as the request body is constructed; the AI mapping stage is the
  // long-running one we can't subdivide without server-side progress events.
  els.uploadForm.hidden = true;
  els.uploadProgress.hidden = false;
  els.uploadCancel.disabled = true;
  setLoading(els.uploadSubmit, true);
  for (const stage of els.uploadProgress.querySelectorAll(".stage")) {
    if (stage.dataset.stage === "read" || stage.dataset.stage === "extract") {
      stage.dataset.state = "done";
    }
  }

  const formData = new FormData();
  formData.append("title", els.uploadTitle.value.trim());
  formData.append("pdf", uploadFile, uploadFile.name);

  try {
    const res = await authedFetch("/api/templates/upload", {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`upload failed (${res.status}): ${detail}`);
    }
    const data = await res.json();
    // Mark every stage done before closing the modal.
    for (const stage of els.uploadProgress.querySelectorAll(".stage")) {
      stage.dataset.state = "done";
    }
    toast(`Uploaded "${data.title}" — ${data.field_count} fields mapped`, "success", 3500);
    closeUploadModal();
    // Reload template list and auto-check the new one.
    await loadTemplates({ autoCheck: data.id });
  } catch (e) {
    toast(e.message || "upload failed", "error", 6000);
    // Roll back to the form view so the user can retry.
    els.uploadForm.hidden = false;
    els.uploadProgress.hidden = true;
    els.uploadCancel.disabled = false;
    setLoading(els.uploadSubmit, false);
  }
}

if (els.uploadTemplateBtn) {
  els.uploadTemplateBtn.addEventListener("click", openUploadModal);
}
if (els.uploadModal) {
  els.uploadModal.querySelectorAll("[data-close]").forEach((el) => {
    el.addEventListener("click", () => {
      // Don't let the user close the modal mid-upload — wait for the
      // OpenAI call to either resolve or reject so we don't orphan a row.
      if (els.uploadCancel.disabled) return;
      closeUploadModal();
    });
  });
}
if (els.uploadFileInput) {
  els.uploadFileInput.addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) setUploadFile(file);
  });
}
if (els.uploadDrop) {
  els.uploadDrop.addEventListener("click", (e) => {
    // Clicking the label triggers the input via for=, but clicks on inner
    // elements bubble strangely; normalize.
    if (e.target.tagName !== "INPUT") {
      e.preventDefault();
      els.uploadFileInput.click();
    }
  });
  els.uploadDrop.addEventListener("dragover", (e) => {
    e.preventDefault();
    els.uploadDrop.classList.add("dragover");
  });
  els.uploadDrop.addEventListener("dragleave", () => {
    els.uploadDrop.classList.remove("dragover");
  });
  els.uploadDrop.addEventListener("drop", (e) => {
    e.preventDefault();
    els.uploadDrop.classList.remove("dragover");
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    if (!file.type.includes("pdf") && !file.name.toLowerCase().endsWith(".pdf")) {
      toast("Only PDF files are supported", "error");
      return;
    }
    setUploadFile(file);
  });
}
if (els.uploadTitle) {
  els.uploadTitle.addEventListener("input", refreshUploadSubmit);
}
if (els.uploadSubmit) {
  els.uploadSubmit.addEventListener("click", submitUpload);
}

// Close upload modal on Escape (mirrors the drawer behavior)
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.uploadModal.hidden && !els.uploadCancel.disabled) {
    closeUploadModal();
  }
});

// ---------- bootstrap ----------
async function bootstrap() {
  // Auth gate: hit /api/auth/me before doing anything. 401 → /login.
  // Doing this BEFORE loadTemplates avoids the visual flash where the doc
  // list briefly tries to render then gets redirected.
  try {
    const res = await fetch("/api/auth/me");
    if (res.status === 401) {
      window.location.href = "/login";
      return;
    }
    if (!res.ok) {
      // Server error on auth probe — show a toast and keep going. The user
      // will hit a 401 elsewhere if they're really not authenticated.
      toast("Could not verify login. Some features may not work.", "error");
    } else {
      const me = await res.json();
      // Stash the email so we can show it in the agent-profile drawer.
      window.__memoirUser = me;
      const slot = document.getElementById("logged-in-email");
      if (slot && me.email) slot.textContent = me.email;
    }
  } catch (e) {
    // Network error or similar; let the user see the app shell and they'll
    // bump into auth errors on the next API call.
    console.error("auth probe failed", e);
  }

  loadProfile();
  loadTemplates();
  updateGenerateBar();

  // Modules (workstreams A, B, C). Defaults must init first so chips.js
  // can read its allow-list when rendering.
  await initDefaults({
    authedFetch,
    toast,
    onDefaultsChanged: () => {
      // Defaults changed (saved or removed) — repaint chips so the passive
      // tick + save-default affordances refresh.
      const chipStrip = document.getElementById("chip-strip");
      if (chipStrip) reconcileFromPayload(collectFields());
    },
  });

  const chipStrip = document.getElementById("chip-strip");
  if (chipStrip) {
    initChips(chipStrip, {
      onChipEdit: (path, newValue) => {
        // Write back into the accordion's data-path input — that is the
        // canonical source of truth (populateFields/collectFields both go
        // through it). Then echo back to the chip strip so it shows the
        // user-edited state.
        const inputs = document.querySelectorAll(`[data-path="${CSS.escape(path)}"]`);
        inputs.forEach((input) => {
          input.value = newValue;
          input.classList.remove("from-extract");
        });
        updateChipFromField(path, newValue);
        fieldsPendingEdits = true;
        if (els.saveFieldsBtn) els.saveFieldsBtn.hidden = false;
        updateGenerateBar();
      },
    });

    // "Save as default" affordance is rendered inside each eligible chip;
    // one delegated handler catches click + keyboard activation across the
    // whole strip. Uses dataset.saveValue (snapshotted at render time) over
    // a fresh DOM read so the toast's value matches the tooltip's value
    // even if the user is mid-edit.
    const handleSaveDefault = async (target) => {
      const path = target.dataset.saveDefaultFor;
      const value = target.dataset.saveValue || "";
      const label = (target.dataset.saveLabel || path).toLowerCase();
      if (!path || !value) return;
      const ok = await saveAsDefault(path, value, { silent: true });
      if (!ok) return;
      // Single rich toast: names the field + value so the user knows the
      // consequence, plus one-click Undo for a 6s window. silent:true on
      // saveAsDefault and removeDefault prevents duplicate generic toasts.
      toast(
        `${label.charAt(0).toUpperCase() + label.slice(1)} will pre-fill as "${value}" on new deals.`,
        "success",
        6000,
        {
          action: {
            label: "Undo",
            // Not silent: removeDefault's own error toast surfaces if the
            // DELETE fails, otherwise the user gets no feedback at all on
            // failure (success is implicit from the chip re-rendering).
            handler: async () => {
              const undone = await removeDefault(path);
              if (undone) reconcileFromPayload(collectFields());
            },
          },
        },
      );
      // Re-render so the bookmark icon disappears and a passive tick appears.
      reconcileFromPayload(collectFields());
    };
    chipStrip.addEventListener("click", (e) => {
      const target = e.target.closest("[data-save-default-for]");
      if (!target) return;
      e.stopPropagation();  // don't trigger chip edit-mode
      handleSaveDefault(target);
    });
    chipStrip.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const target = e.target.closest("[data-save-default-for]");
      if (!target) return;
      e.preventDefault();
      e.stopPropagation();
      handleSaveDefault(target);
    });
  }

  const voiceBtn = document.getElementById("voice-btn");
  if (voiceBtn) {
    initVoice({
      button: voiceBtn,
      textarea: els.notes,
      authedFetch,
      toast,
      onTranscriptAppended: () => {
        // Voice transcript was just appended to the notes — fire a live
        // extract immediately (skip the debounce; the user clearly finished
        // a thought).
        if (_liveDebounceTimer) clearTimeout(_liveDebounceTimer);
        runLiveExtract();
      },
    });
  }

  // Debounced live extract on every notes edit. Existing keydown handlers
  // (Cmd+Enter to fire full extract) keep working — this listener is purely
  // additive.
  if (els.notes) {
    els.notes.addEventListener("input", scheduleLiveExtract);
    els.notes.addEventListener("paste", () => {
      // On paste, run immediately (no debounce) — the user just dropped a
      // full chunk of context in.
      if (_liveDebounceTimer) clearTimeout(_liveDebounceTimer);
      setTimeout(runLiveExtract, 0);
    });
  }
}

// Wire the logout button if present.
const logoutBtn = document.getElementById("logout-btn");
if (logoutBtn) logoutBtn.addEventListener("click", logout);

bootstrap();
