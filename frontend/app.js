// Frontend logic — wires the dashboard to the FastAPI backend, with an
// in-place PDF.js preview after generation.
//
// Flow:
//   notes + images → POST /api/extract → populate parsed-fields panel
//   parsed fields + agent profile + checked docs → POST /api/generate
//     → right pane swaps from selection-mode to preview-mode with one tab
//        per generated doc and a PDF.js render of the active doc
//   "← Edit selection" returns to selection-mode (cache preserved)

import * as pdfjs from "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs";
pdfjs.GlobalWorkerOptions.workerSrc =
  "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs";

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
  tabStrip: document.getElementById("tab-strip"),
  pagesScroll: document.getElementById("pages-scroll"),
  pagesLoading: document.getElementById("pages-loading"),
};

let attachedImages = []; // File[]
let extracted = false;

// Preview-mode state
let lastGenerated = []; // last /api/generate response (array of {document, filename, base64, content_type})
let activeDocKey = null;
const renderedPages = new Map(); // docKey → array of <div.pdf-page> nodes (cache)

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

// ---------- PDF.js preview ----------
function base64ToArrayBuffer(b64) {
  const bin = atob(b64);
  const buf = new ArrayBuffer(bin.length);
  const view = new Uint8Array(buf);
  for (let i = 0; i < bin.length; i++) view[i] = bin.charCodeAt(i);
  return buf;
}

async function renderDocument(doc) {
  if (renderedPages.has(doc.document)) return renderedPages.get(doc.document);
  const data = base64ToArrayBuffer(doc.base64);
  const pdf = await pdfjs.getDocument({ data }).promise;
  const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  const pages = [];
  for (let i = 1; i <= pdf.numPages; i++) {
    const page = await pdf.getPage(i);
    const viewport = page.getViewport({ scale: 1.5 * dpr });
    const canvas = document.createElement("canvas");
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    canvas.style.width = `${Math.min(820, viewport.width / dpr)}px`;
    canvas.style.height = "auto";
    const wrapper = document.createElement("div");
    wrapper.className = "pdf-page";
    wrapper.appendChild(canvas);
    await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
    pages.push(wrapper);
  }
  renderedPages.set(doc.document, pages);
  return pages;
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
  const cached = renderedPages.has(docKey);
  els.pagesLoading.hidden = cached;

  try {
    const pages = await renderDocument(doc);
    if (activeDocKey !== docKey) return; // user switched away mid-render
    pages.forEach((p) => els.pagesScroll.appendChild(p));
    els.pagesScroll.scrollTop = 0;
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

  setActiveTab(next);

  // Pre-warm cache for the other tabs in the background.
  for (const d of docs) {
    if (d.document !== next && !renderedPages.has(d.document)) {
      renderDocument(d).catch((e) => console.error("background render failed", d.document, e));
    }
  }
}

function exitPreviewMode() {
  els.previewMode.hidden = true;
  els.selectionMode.hidden = false;
}

// ---------- API calls ----------
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
  document.querySelectorAll(".doc-card").forEach((card) => {
    const checked = card.querySelector('input[type="checkbox"]').checked;
    if (!checked) return;
    const key = card.dataset.doc;
    if (IMPLEMENTED_DOCS.has(key)) allSelected.push(key);
    else skipped.push(key);
  });

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

    // Bust the render cache for any docs we just regenerated so they re-render
    // with the new content (otherwise we'd show the previous PDF's pages).
    for (const doc of data.documents) renderedPages.delete(doc.document);

    enterPreviewMode(data.documents);

    if (skipped.length > 0) {
      toast(`Skipped (not implemented yet): ${skipped.join(", ")}`, "info", 4500);
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
els.backBtn.addEventListener("click", exitPreviewMode);

els.notes.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    runExtract();
  }
});

els.fields.addEventListener("input", () => {
  if (extracted) {
    computeReadinessAfterExtract();
    updateGenerateBar();
  }
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
updateGenerateBar();
