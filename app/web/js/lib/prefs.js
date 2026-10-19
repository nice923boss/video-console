// Per-viewer UI preferences kept in localStorage. Storage may be unavailable (private window, cleared
// site data), so reads fall back to the default and writes are best effort.
import { useState } from "./h.js";

export function readPref(key, fallback, valid) {
  try {
    const v = JSON.parse(localStorage.getItem(key));
    return valid(v) ? v : fallback;
  } catch {
    return fallback;
  }
}

export function usePref(key, fallback, valid) {
  const [value, setValue] = useState(() => readPref(key, fallback, valid));
  const set = (next) => {
    setValue(next);
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {
      // Convenience only.
    }
  };
  return [value, set];
}
