// Uncaught errors in the page go to the server's app.log (QA-09), so the debug bundle on the settings page
// has them too; without this they vanish with the window.
import { api, ApiError } from "./api.js";

const MAX_REPORTS = 20; // per page load, so an error in a render loop cannot flood the log
let sent = 0;

// Also for a problem the page notices without an exception; stack then holds its details.
export function report(message, stack) {
  if (sent >= MAX_REPORTS) return;
  sent += 1;
  api.post("/api/system/client-error", {
    message: String(message || "").slice(0, 500),
    stack: String(stack || "").slice(0, 4000),
    view: location.hash.slice(1),
  }).catch((err) => console.warn("無法回報畫面錯誤", err));
}

export function watchErrors() {
  window.addEventListener("error", (e) => report(e.message, e.error && e.error.stack));
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    if (r instanceof ApiError) return; // the server logged its side already
    report((r && r.message) || r, r && r.stack);
  });
}
