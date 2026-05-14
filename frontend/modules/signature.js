// Signature capture — Type / Draw / Save flow.
//
// One module owns:
//   - opening + closing the modal (with the right mode/title for signature vs initials)
//   - typed-cursive rendering → PNG via an offscreen canvas
//   - trackpad/mouse drawing on the visible canvas
//   - save-to-defaults via PUT /api/me/defaults/agent.signature (and .initials)
//
// PNGs are stored as base64 data: URLs everywhere on the frontend (the
// agent-defaults endpoint accepts a string). We trim the data: prefix when
// sending so the stored value is just the base64 payload, matching the
// backend's base64.b64decode contract.
//
// Lazy-loads Google Fonts on first modal open — keeps the cold load
// light for agents who haven't set up a signature.

const SIG_FONT_FAMILIES = ["Caveat", "Great Vibes", "Sacramento"];
const FONT_LINK_ID = "signature-fonts-link";
let _fontsLoaded = false;

let _authedFetch = null;
let _toast = null;
let _saveDefault = null;   // (path, value) → Promise<bool>
let _state = {
  mode: "agent",           // 'agent' | 'initials'
  tab: "type",             // 'type' | 'draw'
  font: "Caveat",
  drawnDataUrl: null,      // base64 data URL from the draw canvas
  agentName: "",           // pre-fills the type input
};

// Cached DOM refs — looked up once on init.
const $ = {};

function ensureFontsLoaded() {
  if (_fontsLoaded) return;
  _fontsLoaded = true;
  if (document.getElementById(FONT_LINK_ID)) return;
  const link = document.createElement("link");
  link.id = FONT_LINK_ID;
  link.rel = "stylesheet";
  link.href = "https://fonts.googleapis.com/css2?family=Caveat:wght@600&family=Great+Vibes&family=Sacramento&display=swap";
  document.head.appendChild(link);
}

// -- Type tab: render the agent's typed name into a PNG via an offscreen canvas.
//
// Why not just store the typed string? Because pdf_fill stamps a PNG via
// signature_stamp.py — the backend doesn't render type-cursive itself. We
// rasterize at capture time so the same PNG is stamped on every form.
function renderTypedToPng(text, fontFamily) {
  const w = 600;
  const h = 200;
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  // Transparent background — signature_stamp's reportlab pipeline uses the
  // PNG's alpha channel as a soft mask, so a transparent BG renders
  // correctly over the destination PDF.
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#000";
  // Use the cursive font; fall back to system cursive if Google Fonts
  // hasn't loaded yet (rare — fontsLoaded promises are racy, this is just
  // a safety net).
  ctx.font = `bold 120px "${fontFamily}", cursive`;
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  // Shrink the font if the text would overflow the canvas at 120px.
  let fontSize = 120;
  while (ctx.measureText(text).width > w - 40 && fontSize > 30) {
    fontSize -= 4;
    ctx.font = `bold ${fontSize}px "${fontFamily}", cursive`;
  }
  // Vertical position: baseline at ~75% of canvas height, gives room for
  // descenders without bottom-clipping.
  ctx.fillText(text, 20, h * 0.75);
  return canvas.toDataURL("image/png");
}

function dataUrlToBase64(dataUrl) {
  // "data:image/png;base64,XXXX" → "XXXX"
  const i = dataUrl.indexOf(",");
  return i >= 0 ? dataUrl.slice(i + 1) : dataUrl;
}

function base64ToDataUrl(base64) {
  return `data:image/png;base64,${base64}`;
}

// -- Draw tab: pointer events on a 600x200 canvas with a smooth stroke.

function attachCanvasDrawing(canvas) {
  const ctx = canvas.getContext("2d");
  // The canvas attribute size (600x200) is the rasterization grid. The
  // CSS sizes it to fill the container. Pointer coords need to be mapped
  // from CSS space → canvas space.
  let drawing = false;
  let lastX = 0;
  let lastY = 0;
  let hasContent = false;

  function setupStroke() {
    ctx.lineWidth = 2.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#000";
  }
  setupStroke();

  function pointerPos(e) {
    const r = canvas.getBoundingClientRect();
    const x = (e.clientX - r.left) * (canvas.width / r.width);
    const y = (e.clientY - r.top) * (canvas.height / r.height);
    return { x, y };
  }

  function start(e) {
    drawing = true;
    const p = pointerPos(e);
    lastX = p.x;
    lastY = p.y;
    // Track a single-dot tap for users who tap-and-release.
    ctx.beginPath();
    ctx.arc(p.x, p.y, 1.25, 0, Math.PI * 2);
    ctx.fillStyle = "#000";
    ctx.fill();
    e.preventDefault();
  }
  function move(e) {
    if (!drawing) return;
    const p = pointerPos(e);
    ctx.beginPath();
    ctx.moveTo(lastX, lastY);
    ctx.lineTo(p.x, p.y);
    setupStroke();
    ctx.stroke();
    lastX = p.x;
    lastY = p.y;
    hasContent = true;
    canvas.parentElement.classList.add("has-content");
    e.preventDefault();
  }
  function end() {
    if (!drawing) return;
    drawing = false;
    if (hasContent) {
      _state.drawnDataUrl = canvas.toDataURL("image/png");
    }
  }

  canvas.addEventListener("pointerdown", start);
  canvas.addEventListener("pointermove", move);
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("pointerleave", end);

  function clear() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    hasContent = false;
    _state.drawnDataUrl = null;
    canvas.parentElement.classList.remove("has-content");
  }
  return { clear };
}

// -- Modal open / close ----------------------------------------------------

function setTab(tab) {
  _state.tab = tab;
  for (const btn of document.querySelectorAll(".signature-tab")) {
    btn.classList.toggle("is-active", btn.dataset.sigTab === tab);
  }
  for (const pane of document.querySelectorAll(".signature-pane")) {
    pane.hidden = pane.dataset.sigPane !== tab;
  }
}

function setFont(font) {
  _state.font = font;
  for (const btn of document.querySelectorAll(".signature-font-option")) {
    btn.classList.toggle("is-active", btn.dataset.sigFont === font);
  }
  // Inline-style the preview so each font swap takes effect without a CSS round-trip.
  $.previewText.style.fontFamily = `"${font}", cursive`;
}

function updateTypePreview() {
  const text = $.typeInput.value || _state.agentName || "";
  $.previewText.textContent = text;
}

function openModal({ mode = "agent", currentBase64 = null, agentName = "" } = {}) {
  ensureFontsLoaded();
  _state.mode = mode;
  _state.drawnDataUrl = null;
  _state.agentName = agentName;

  // Title + label vary by mode.
  if (mode === "initials") {
    $.title.textContent = "Set your initials";
    $.sub.textContent = "Stamped at the page footer on every form.";
    $.typeLabel.textContent = "Your initials";
    // Pre-fill with first letters of each word in agent name.
    const initials = (agentName || "")
      .split(/\s+/)
      .filter(Boolean)
      .map(w => w[0].toUpperCase())
      .join("");
    $.typeInput.value = initials;
    $.typeInput.placeholder = "JD";
  } else {
    $.title.textContent = "Set your signature";
    $.sub.textContent = "Stamped on every form you generate. Clients still sign separately.";
    $.typeLabel.textContent = "Your name";
    $.typeInput.value = agentName || "";
    $.typeInput.placeholder = "Jane Doe";
  }

  // Reset draw canvas.
  const drawCtx = $.canvas.getContext("2d");
  drawCtx.clearRect(0, 0, $.canvas.width, $.canvas.height);
  $.canvas.parentElement.classList.remove("has-content");
  // If they already have a signature saved + we're editing, pre-stamp the
  // existing PNG onto the canvas so they can extend rather than redraw.
  if (currentBase64) {
    const img = new Image();
    img.onload = () => {
      drawCtx.drawImage(img, 0, 0, $.canvas.width, $.canvas.height);
      $.canvas.parentElement.classList.add("has-content");
      _state.drawnDataUrl = $.canvas.toDataURL("image/png");
    };
    img.src = base64ToDataUrl(currentBase64);
  }

  setTab("type");
  setFont("Caveat");
  updateTypePreview();
  $.modal.hidden = false;
}

function closeModal() {
  $.modal.hidden = true;
}

// -- Save ------------------------------------------------------------------

async function save() {
  const path = _state.mode === "initials" ? "agent.initials" : "agent.signature";

  let dataUrl;
  if (_state.tab === "draw") {
    dataUrl = _state.drawnDataUrl;
    if (!dataUrl) {
      _toast("Draw your signature first", "error");
      return;
    }
  } else {
    const text = ($.typeInput.value || "").trim();
    if (!text) {
      _toast(`Type your ${_state.mode === "initials" ? "initials" : "name"} first`, "error");
      return;
    }
    dataUrl = renderTypedToPng(text, _state.font);
  }

  const base64 = dataUrlToBase64(dataUrl);

  $.saveBtn.disabled = true;
  $.saveBtn.querySelector(".btn-spinner").hidden = false;
  try {
    const ok = await _saveDefault(path, base64, { silent: true });
    if (ok) {
      _toast(_state.mode === "initials" ? "Initials saved" : "Signature saved", "success", 2000);
      // Refresh the thumbnail in the agent profile drawer.
      refreshThumbnails({ [path]: base64 });
      closeModal();
    }
  } finally {
    $.saveBtn.disabled = false;
    $.saveBtn.querySelector(".btn-spinner").hidden = true;
  }
}

function refreshThumbnails(defaults) {
  const sig = defaults["agent.signature"];
  const ini = defaults["agent.initials"];
  const sigImg = document.getElementById("signature-thumb-img");
  const sigEmpty = document.getElementById("signature-thumb-empty");
  if (sig) {
    sigImg.src = base64ToDataUrl(sig);
    sigImg.hidden = false;
    sigEmpty.hidden = true;
  } else {
    sigImg.hidden = true;
    sigEmpty.hidden = false;
  }
  const iniImg = document.getElementById("initials-thumb-img");
  const iniEmpty = document.getElementById("initials-thumb-empty");
  if (ini) {
    iniImg.src = base64ToDataUrl(ini);
    iniImg.hidden = false;
    iniEmpty.hidden = true;
  } else {
    iniImg.hidden = true;
    iniEmpty.hidden = false;
  }
}

// -- Public API ------------------------------------------------------------

export function initSignature({ authedFetch, toast, saveDefault, getDefaults, getAgentName }) {
  _authedFetch = authedFetch;
  _toast = toast || (() => {});
  _saveDefault = saveDefault;

  $.modal = document.getElementById("signature-modal");
  $.title = document.getElementById("signature-modal-title");
  $.sub = document.getElementById("signature-modal-sub");
  $.typeLabel = document.getElementById("signature-type-label");
  $.typeInput = document.getElementById("signature-type-input");
  $.previewText = document.getElementById("signature-preview-text");
  $.canvas = document.getElementById("signature-canvas");
  $.saveBtn = document.getElementById("signature-save-btn");

  if (!$.modal) return; // markup missing — nothing to wire

  // Modal close handlers (scrim, X button, Cancel).
  $.modal.querySelectorAll("[data-close]").forEach(el => {
    el.addEventListener("click", closeModal);
  });

  // Tabs.
  for (const btn of document.querySelectorAll(".signature-tab")) {
    btn.addEventListener("click", () => setTab(btn.dataset.sigTab));
  }
  // Fonts.
  for (const btn of document.querySelectorAll(".signature-font-option")) {
    btn.addEventListener("click", () => setFont(btn.dataset.sigFont));
  }
  // Type input live preview.
  $.typeInput.addEventListener("input", updateTypePreview);

  // Draw canvas.
  const drawing = attachCanvasDrawing($.canvas);
  document.getElementById("signature-clear-btn").addEventListener("click", drawing.clear);

  // Save button.
  $.saveBtn.addEventListener("click", save);

  // Thumbnails in the agent profile drawer.
  const sigThumb = document.getElementById("signature-thumb");
  const iniThumb = document.getElementById("initials-thumb");
  if (sigThumb) {
    sigThumb.addEventListener("click", () => {
      const d = getDefaults();
      openModal({
        mode: "agent",
        currentBase64: d["agent.signature"] || null,
        agentName: getAgentName() || "",
      });
    });
  }
  if (iniThumb) {
    iniThumb.addEventListener("click", () => {
      const d = getDefaults();
      openModal({
        mode: "initials",
        currentBase64: d["agent.initials"] || null,
        agentName: getAgentName() || "",
      });
    });
  }

  // Initial paint: if defaults already have signatures, show their thumbs.
  refreshThumbnails(getDefaults());
}

export function openSignatureModal(opts) {
  openModal(opts || {});
}

export { refreshThumbnails };
