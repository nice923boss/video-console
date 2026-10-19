// Applies ui.theme (dark | light | system) to <html data-theme>.

let media = null;
let preference = "dark";

function resolved() {
  if (preference !== "system") return preference === "light" ? "light" : "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function paint() {
  document.documentElement.dataset.theme = resolved();
}

export function applyTheme(settings) {
  preference = (settings && settings.ui && settings.ui.theme) || "dark";
  if (preference === "system" && !media) {
    media = window.matchMedia("(prefers-color-scheme: light)");
    media.addEventListener("change", paint);
  }
  paint();
}

export function currentTheme() {
  return resolved();
}
