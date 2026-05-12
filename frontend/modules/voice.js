// Voice intake — workstream B.
//
// Hold-to-talk button uses MediaRecorder (browser-native, no extra deps) to
// capture audio. On release the blob is POSTed to /api/transcribe; the
// returned text is appended to the notes textarea, which triggers the
// debounced live extraction path in chips/app.js — no extra wiring.
//
// States (from plan-design-review pass 2):
//   idle               - default; small mic glyph
//   awaiting-permission - first hold, browser is prompting for mic access
//   permission-denied   - user said no; button replaced with link to fix
//   recording           - red ring pulse, "0:08" elapsed timer
//   countdown           - last 30s before 90s hard cap; timer turns amber
//   transcribing        - audio uploaded, waiting on Whisper
//   error               - non-fatal failure; toast + revert to idle
//
// Hard caps (mirror server, see backend/agent_defaults.py):
//   90s audio duration (auto-stop at 90s, countdown visible after 60s)
//   5MB body (we never get close — opus at this bitrate is ~10kB/s)

const MAX_SECONDS = 90;
const COUNTDOWN_AFTER_SECONDS = 60;

let _button = null;
let _textarea = null;
let _authedFetch = null;
let _toast = null;
let _onTranscriptAppended = null;

let _recorder = null;
let _chunks = [];
let _startedAt = 0;
let _autoStopTimer = null;
let _tickTimer = null;
let _permissionDenied = false;
let _busy = false;

// ---- public API ----

export function initVoice({ button, textarea, authedFetch, toast, onTranscriptAppended }) {
  _button = button;
  _textarea = textarea;
  _authedFetch = authedFetch;
  _toast = toast || (() => {});
  _onTranscriptAppended = onTranscriptAppended || (() => {});

  if (!_button) return;
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    // Browser doesn't support recording at all — Safari < 14, ancient Firefox.
    // Hide the button so the user isn't presented with a broken affordance.
    _button.hidden = true;
    console.warn("voice: MediaRecorder unavailable; button hidden");
    return;
  }

  // pointerdown / pointerup over plain mousedown so we get mobile touch
  // for free. preventDefault keeps the button from selecting text on long press.
  // setPointerCapture inside onPress routes pointerup to this element no matter
  // where the cursor/finger ends up — without it pointerleave would stop the
  // recording the moment the user drifts off the button (the bug the user hit
  // on 2026-05-11). pointercancel is the OS yanking the pointer (incoming call,
  // app switch, browser dialog) — treat that as cancel, not commit.
  _button.addEventListener("pointerdown", onPress);
  _button.addEventListener("pointerup", onRelease);
  _button.addEventListener("pointercancel", onRelease);

  setState("idle", "Hold to talk");
}

// ---- internals ----

async function onPress(e) {
  if (_busy) return;
  e.preventDefault();
  if (_permissionDenied) {
    _toast("Microphone access is blocked. Enable it in your browser site settings.", "error", 5000);
    return;
  }
  // Capture the pointer so pointerup fires on this button even if the cursor
  // or finger has drifted off it by release time. Without capture the user
  // had to keep hovering exactly on the button to keep recording.
  try { _button.setPointerCapture(e.pointerId); } catch (_) { /* ancient browser */ }
  _busy = true;

  // Ask for the mic. The browser only shows the prompt once per origin; after
  // that getUserMedia resolves immediately if the user already said yes.
  let stream;
  try {
    setState("awaiting-permission", "Allow microphone…");
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    _permissionDenied = err && (err.name === "NotAllowedError" || err.name === "PermissionDeniedError");
    if (_permissionDenied) {
      setState("permission-denied", "Mic blocked");
      _toast("You'll need to allow microphone access to use voice.", "error", 5000);
    } else {
      setState("idle", "Hold to talk");
      _toast("Couldn't access microphone", "error");
    }
    _busy = false;
    return;
  }

  // Pick the most-broadly-supported container. webm/opus on Chrome, mp4 on Safari.
  // We don't transcode — Whisper handles both.
  const mimeType = pickMimeType();
  try {
    _recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  } catch (err) {
    stopStream(stream);
    setState("idle", "Hold to talk");
    _toast("Couldn't start recording", "error");
    _busy = false;
    return;
  }

  _chunks = [];
  _startedAt = Date.now();
  _recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) _chunks.push(e.data);
  };
  _recorder.onstop = async () => {
    stopStream(stream);
    if (_tickTimer) {
      clearInterval(_tickTimer);
      _tickTimer = null;
    }
    if (_autoStopTimer) {
      clearTimeout(_autoStopTimer);
      _autoStopTimer = null;
    }

    const elapsed = Math.max(1, Math.round((Date.now() - _startedAt) / 1000));
    if (elapsed < 1 || _chunks.length === 0) {
      setState("idle", "Hold to talk");
      _busy = false;
      return;
    }
    const blob = new Blob(_chunks, { type: mimeType || "audio/webm" });
    await uploadTranscript(blob, elapsed, mimeType);
    _busy = false;
  };
  _recorder.start();

  setState("recording", "● 0:00");
  // Tick the timer every 500ms; switch into countdown styling after the
  // first minute so the user sees the cap approaching.
  _tickTimer = setInterval(() => {
    const seconds = Math.round((Date.now() - _startedAt) / 1000);
    const mm = String(Math.floor(seconds / 60));
    const ss = String(seconds % 60).padStart(2, "0");
    const mode = seconds >= COUNTDOWN_AFTER_SECONDS ? "countdown" : "recording";
    setState(mode, `● ${mm}:${ss}`);
  }, 500);

  // Hard cap auto-stop: stop after MAX_SECONDS regardless of release.
  _autoStopTimer = setTimeout(() => {
    if (_recorder && _recorder.state === "recording") {
      _toast("Hit the 90-second voice cap — uploading what we have", "info", 3000);
      try { _recorder.stop(); } catch (_) {}
    }
  }, MAX_SECONDS * 1000);
}

function onRelease(e) {
  if (!_recorder || _recorder.state !== "recording") return;
  e.preventDefault();
  try { _recorder.stop(); } catch (_) {}
}

async function uploadTranscript(blob, durationSeconds, mimeType) {
  setState("transcribing", "Transcribing…");

  const requestId = cryptoRandomId();
  const ext = (mimeType || "audio/webm").split("/")[1].split(";")[0] || "webm";
  const formData = new FormData();
  formData.append("audio", blob, `clip.${ext}`);
  formData.append("request_id", requestId);
  formData.append("duration_seconds", String(durationSeconds));

  try {
    const res = await _authedFetch("/api/transcribe", { method: "POST", body: formData });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      if (res.status === 429) {
        _toast("You've hit today's voice quota. Try typing for now.", "error", 5000);
      } else if (res.status === 413) {
        _toast("Recording was too large to send", "error");
      } else {
        _toast(`Voice failed: ${detail}`, "error", 4000);
      }
      setState("idle", "Hold to talk");
      return;
    }
    const data = await res.json();
    const transcript = (data.transcript || "").trim();
    if (!transcript) {
      _toast("Couldn't hear anything — try again", "info", 2500);
      setState("idle", "Hold to talk");
      return;
    }

    appendToTextarea(transcript);
    _onTranscriptAppended(transcript);
    setState("idle", "Hold to talk");
  } catch (e) {
    console.error("voice: upload failed", e);
    _toast("Network error while sending audio", "error");
    setState("idle", "Hold to talk");
  }
}

function appendToTextarea(text) {
  if (!_textarea) return;
  const cur = _textarea.value;
  const trimmed = cur.trimEnd();
  const sep = trimmed ? "\n" : "";
  _textarea.value = `${trimmed}${sep}${text}`;
  // Move caret to end + dispatch input so any debounced listeners fire.
  _textarea.dispatchEvent(new Event("input", { bubbles: true }));
  _textarea.focus();
  _textarea.scrollTop = _textarea.scrollHeight;
}

function setState(name, label) {
  if (!_button) return;
  _button.dataset.voiceState = name;
  const labelEl = _button.querySelector(".voice-btn-label");
  if (labelEl) labelEl.textContent = label;
  else _button.textContent = label;
  _button.setAttribute("aria-label", `Voice: ${label}`);
  _button.disabled = name === "transcribing" || name === "awaiting-permission";
}

function stopStream(stream) {
  try {
    stream.getTracks().forEach((t) => t.stop());
  } catch (_) { /* noop */ }
}

function pickMimeType() {
  // Pick the most-compatible MIME the browser supports.
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  for (const m of candidates) {
    if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
  }
  return null;
}

function cryptoRandomId() {
  if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  return `r-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
