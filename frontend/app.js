// Frontend logic — wires the dashboard to the FastAPI backend, with an
// in-place PDF preview after generation.
//
// Flow:
//   notes + images → POST /api/extract → populate parsed-fields panel
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

const IMPLEMENTED_DOCS = new Set(["lease_invoice", "lease_abstract", "tenant_rep", "multiboard"]);

const FRIENDLY = {
  lease_invoice: "Lease Invoice",
  lease_abstract: "Lease Abstract",
  tenant_rep: "Tenant Rep",
  multiboard: "Multi-Board Contract",
};

const els = {
  notesWrap: document.getElementById("notes-wrap"),
  notes: document.getElementById("notes"),
  attachments: document.getElementById("attachments"),
  attachBtn: document.getElementById("attach-btn"),
  fileInput: document.getElementById("file-input"),
  extractBtn: document.getElementById("extract-btn"),
  fields: document.getElementById("fields"),
  fieldsStatus: document.getElementById("fields-status"),
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
};

let attachedImages = []; // File[]
let extracted = false;

// Preview-mode state
let lastGenerated = []; // last /api/generate response (array of {document, filename, base64, content_type})
let activeDocKey = null;
// Per-doc preview state (image renders + pending edits) lives in docState below.

// ---------- toasts ----------
function toast(message, type = "info", ms = 4000) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
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

// ---------- doc card readiness (selection mode) ----------
function setReadiness(card, level, text) {
  const pill = card.querySelector(".readiness");
  if (!pill) return;
  pill.className = `readiness readiness-${level}`;
  pill.textContent = text;
}

function computeReadinessAfterExtract() {
  const fields = collectFields();
  const profile = readProfileFromInputs();
  const have = (path) => {
    const v = getByPath({ ...fields, agent: profile }, path);
    return Array.isArray(v) ? v.length > 0 : (v != null && String(v).trim() !== "");
  };

  const checks = {
    lease_invoice:  ["property.address", "property.city", "lease_start", "tenant_or_buyer_names", "commission_amount", "agent.name"],
    lease_abstract: ["property.address", "property.city", "lease_start", "lease_end", "monthly_rent", "commission_amount", "agent.name"],
    tenant_rep:     ["property.address", "property.city", "tenant_or_buyer_names", "commission_amount", "agent.name"],
    multiboard:     ["property.address", "property.city", "tenant_or_buyer_names", "seller_names", "purchase_price", "earnest_money", "closing_date", "agent.name"],
  };

  document.querySelectorAll(".doc-card").forEach((card) => {
    const key = card.dataset.doc;
    if (!IMPLEMENTED_DOCS.has(key)) {
      setReadiness(card, "pending", "coming in next slice");
      return;
    }
    const required = checks[key] || [];
    const missing = required.filter((p) => !have(p));
    if (missing.length === 0) setReadiness(card, "ready", "ready");
    else if (missing.length === required.length) setReadiness(card, "missing", `${missing.length} missing`);
    else setReadiness(card, "partial", `${missing.length} missing`);
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
  els.generateSummary.textContent = `${selected} selected · ${extracted ? ready + " ready" : "0 ready"}`;
  els.generateBtn.disabled = !extracted || selected === 0;
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
  const res = await fetch("/api/preview", {
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
    syncSaveBar(state);
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
    const res = await fetch("/api/edit", {
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

async function setActiveTab(docKey) {
  if (activeDocKey === docKey) return;
  activeDocKey = docKey;

  els.tabStrip.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.doc === docKey);
  });

  const doc = lastGenerated.find((d) => d.document === docKey);
  if (!doc) return;

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

async function runExtract() {
  if (!els.notes.value.trim() && attachedImages.length === 0) {
    els.notes.focus();
    toast("Add notes or attach an image first", "error");
    return;
  }
  setLoading(els.extractBtn, true);
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
    const res = await fetch("/api/extract", { method: "POST", body: formData });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`extract failed (${res.status}): ${detail}`);
    }
    const data = await res.json();
    extracted = true;
    els.fields.hidden = false;
    populateFields(data);
    computeReadinessAfterExtract();
    updateGenerateBar();
    els.fields.scrollIntoView({ behavior: "smooth", block: "nearest" });
    toast("Fields extracted — review before generating", "success", 2500);
  } catch (e) {
    toast(e.message || "extract failed", "error");
    console.error(e);
  } finally {
    setLoading(els.extractBtn, false);
  }
}

async function runGenerate(triggerBtn) {
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
  try {
    const body = {
      fields: collectFields(),
      agent: readProfileFromInputs(),
      documents: allSelected,
    };
    const res = await fetch("/api/generate", {
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
  computeReadinessAfterExtract();
  updateGenerateBar();
});

document.querySelectorAll(".doc-card input[type='checkbox']").forEach((box) => {
  box.addEventListener("change", updateGenerateBar);
});

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
});
els.drawer.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) els.drawer.hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.drawer.hidden) els.drawer.hidden = true;
});

// ---------- bootstrap ----------
loadProfile();
computeReadinessAfterExtract();
updateGenerateBar();
