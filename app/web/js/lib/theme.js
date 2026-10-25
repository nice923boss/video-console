// Applies ui.theme (dark | light | system) to <html data-theme>, and ui.textScale to the --text-scale every
// font size in the CSS is multiplied by.
import { useState, useEffect } from "./h.js";

let media = null;
let preference = "dark";
const listeners = new Set();

function resolved() {
  if (preference !== "system") return preference === "light" ? "light" : "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function paint() {
  document.documentElement.dataset.theme = resolved();
  for (const fn of listeners) fn(resolved());
}

export function applyTheme(settings) {
  preference = (settings && settings.ui && settings.ui.theme) || "dark";
  document.documentElement.style.setProperty("--text-scale", String((settings && settings.ui && settings.ui.textScale) || 1));
  if (preference === "system" && !media) {
    media = window.matchMedia("(prefers-color-scheme: light)");
    media.addEventListener("change", paint);
  }
  paint();
}

export function currentTheme() {
  return resolved();
}

// The look in use, re-rendering the caller when Windows flips light/dark under "system".
export function useTheme() {
  const [theme, setTheme] = useState(resolved);
  useEffect(() => {
    listeners.add(setTheme);
    return () => listeners.delete(setTheme);
  }, []);
  return theme;
}
