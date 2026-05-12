// Per-agent defaults — client side.
//
// One module owns:
//   - what defaults the user has saved (cached after a single GET on init)
//   - which canonical paths are eligible to be saved (the server allow-list)
//   - the save / unsave network calls
//   - dotted-path helpers (setByPath/getByPath) used by chips.js too
//
// The chip-side affordance lives in chips.js — a "+" button next to chips
// whose path is in the allow-list and which haven't been saved yet. Hitting
// that button calls saveAsDefault() here.

let _state = {
  defaults: {},               // {path: value}
  allowedPaths: new Set(),    // set of canonical paths
  loaded: false,              // true after the initial GET completes
};

let _authedFetch = null;
let _toast = null;
let _onDefaultsChanged = null;

// ---- public API ----

export async function initDefaults({ authedFetch, toast, onDefaultsChanged }) {
  _authedFetch = authedFetch;
  _toast = toast || (() => {});
  _onDefaultsChanged = onDefaultsChanged || (() => {});

  try {
    const res = await _authedFetch("/api/me/defaults");
    if (!res.ok) {
      // Non-fatal — the user just loses the defaults affordance for this
      // session. Don't block the rest of the app from loading.
      console.warn("defaults: GET /api/me/defaults failed", res.status);
      _state.loaded = true;
      return;
    }
    const body = await res.json();
    _state.defaults = body.defaults || {};
    _state.allowedPaths = new Set(body.allowed_paths || []);
    _state.loaded = true;
  } catch (e) {
    console.warn("defaults: init failed", e);
    _state.loaded = true;
  }
}

export function getDefaults() {
  return _state.defaults;
}

export function defaultsHas(path) {
  return _state.allowedPaths.has(path);
}

export function isPathDefaulted(path) {
  return Object.prototype.hasOwnProperty.call(_state.defaults, path);
}

export async function saveAsDefault(path, value, { silent = false } = {}) {
  if (!_state.allowedPaths.has(path)) {
    _toast("That field isn't eligible to be saved as a default", "error");
    return false;
  }
  if (!value || !String(value).trim()) {
    _toast("Can't save an empty value as a default", "error");
    return false;
  }
  try {
    const res = await _authedFetch(`/api/me/defaults/${encodeURIComponent(path)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: String(value).trim() }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _state.defaults[path] = String(value).trim();
    // Caller may want to show a richer toast (with Undo, naming the field +
    // value). `silent: true` lets them suppress the generic success message.
    if (!silent) _toast("Saved as your default", "success", 2000);
    _onDefaultsChanged();
    return true;
  } catch (e) {
    console.error("defaults: save failed", e);
    _toast("Couldn't save default — try again", "error");
    return false;
  }
}

export async function removeDefault(path, { silent = false } = {}) {
  try {
    const res = await _authedFetch(`/api/me/defaults/${encodeURIComponent(path)}`, {
      method: "DELETE",
    });
    if (!res.ok && res.status !== 204) throw new Error(`HTTP ${res.status}`);
    delete _state.defaults[path];
    if (!silent) _toast("Default removed", "success", 1500);
    _onDefaultsChanged();
    return true;
  } catch (e) {
    console.error("defaults: remove failed", e);
    _toast("Couldn't remove default", "error");
    return false;
  }
}

// ---- dotted-path helpers (shared with chips.js, app.js can use them too) ----

export function setByPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null || typeof cur[parts[i]] !== "object") cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

export function getByPath(obj, path) {
  let cur = obj;
  for (const part of path.split(".")) {
    if (cur == null) return undefined;
    cur = cur[part];
  }
  return cur;
}
