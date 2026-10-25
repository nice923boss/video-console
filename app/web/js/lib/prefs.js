// Per-viewer UI preferences kept in localStorage. Storage may be unavailable (private window, cleared
// site data), so reads fall back to the default and writes are best effort.
import { useState, useEffect } from "./h.js";

const CHANGED = "vc-pref";

export function readPref(key, fallback, valid) {
  try {
    const v = JSON.parse(localStorage.getItem(key));
    return valid(v) ? v : fallback;
  } catch {
    return fallback;
  }
}

// Also for writes from outside a component (workspace layouts, UI-08): every mounted usePref of the key follows.
export function writePref(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Convenience only.
  }
  window.dispatchEvent(new CustomEvent(CHANGED, { detail: { key, value } }));
}

export function usePref(key, fallback, valid) {
  const [value, setValue] = useState(() => readPref(key, fallback, valid));
  useEffect(() => {
    const follow = (e) => {
      if (e.detail.key === key && valid(e.detail.value)) setValue(e.detail.value);
    };
    window.addEventListener(CHANGED, follow);
    return () => window.removeEventListener(CHANGED, follow);
  }, [key]);
  const set = (next) => {
    setValue(next);
    writePref(key, next);
  };
  return [value, set];
}
