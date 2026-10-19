// Toast notices in the lower right corner. Errors stay longer, sticky notices (duration 0 or a
// progress bar) stay until updated or closed, and hovering the stack pauses every timer.
import { html, useState, useEffect } from "../lib/h.js";
import { Icon } from "./icons.js";

const DEFAULT_MS = 4000;
const ERROR_MS = 8000;
const MAX_VISIBLE = 5;
const LEVEL_ICON = { info: "info", success: "check-circle", error: "x-circle", warn: "alert" };

let items = [];
let seq = 0;
let paused = false;
const timers = new Map();
const subs = new Set();

function emit() {
  for (const fn of [...subs]) fn(items);
}

function lifetime(item) {
  if (item.duration != null) return item.duration;
  if (item.progress != null) return 0;
  return item.level === "error" ? ERROR_MS : DEFAULT_MS;
}

function stopTimer(id) {
  clearTimeout(timers.get(id));
  timers.delete(id);
}

function schedule(item) {
  stopTimer(item.id);
  const ms = lifetime(item);
  if (ms > 0 && !paused) timers.set(item.id, setTimeout(() => dismissToast(item.id), ms));
}

// Drop the oldest notice that would expire anyway, so a running upload stays visible.
function trim() {
  while (items.length > MAX_VISIBLE) {
    let i = items.findIndex((x) => lifetime(x) > 0);
    if (i < 0) i = 0;
    stopTimer(items[i].id);
    items = items.filter((_, k) => k !== i);
  }
}

export function toast(message, { level = "info", title = "", action = null, duration = null, progress = null, id = null } = {}) {
  const key = id || `toast-${(seq += 1)}`;
  const item = { id: key, message: message == null ? "" : String(message), level, title, action, duration, progress };
  items = items.some((x) => x.id === key) ? items.map((x) => (x.id === key ? item : x)) : [...items, item];
  trim();
  schedule(item);
  emit();
  return key;
}

export function updateToast(id, patch) {
  const cur = items.find((x) => x.id === id);
  if (!cur) return;
  const item = { ...cur, ...patch };
  items = items.map((x) => (x.id === id ? item : x));
  if ("duration" in patch || "progress" in patch || "level" in patch) schedule(item);
  emit();
}

export function dismissToast(id) {
  stopTimer(id);
  const before = items.length;
  items = items.filter((x) => x.id !== id);
  if (!items.length) paused = false; // the stack vanished under the pointer, so no mouseleave comes
  if (items.length !== before) emit();
}

export function toastError(err, title = "") {
  if (!err || err.name === "AbortError") return null;
  let message = err.message || String(err);
  if (typeof err.detail === "string" && err.detail && !message.includes(err.detail)) message += `\n${err.detail}`;
  return toast(message, { level: "error", title });
}

function hold(on) {
  paused = on;
  for (const item of items) {
    if (on) stopTimer(item.id);
    else schedule(item);
  }
}

function ToastItem({ t }) {
  const pct = t.progress == null ? null : Math.round(Math.min(1, Math.max(0, t.progress)) * 100);
  const run = () => {
    dismissToast(t.id);
    t.action.fn();
  };
  return html`<div class=${`toast toast-${t.level}`}>
    <${Icon} name=${LEVEL_ICON[t.level] || "info"} size=${18} class="toast-icon" />
    <div class="toast-body">
      ${t.title && html`<div class="toast-title">${t.title}</div>`}
      ${t.message && html`<div class="toast-msg">${t.message}</div>`}
      ${pct != null && html`<div class="toast-progress"><i style=${`width:${pct}%`}></i></div>`}
    </div>
    ${t.action && html`<button type="button" class="toast-action" onClick=${run}>${t.action.label}</button>`}
    <button type="button" class="toast-close" title="關閉" aria-label="關閉" onClick=${() => dismissToast(t.id)}>
      <${Icon} name="x" size=${14} />
    </button>
  </div>`;
}

export function ToastHost() {
  const [list, setList] = useState(items);
  useEffect(() => {
    subs.add(setList);
    setList(items);
    return () => subs.delete(setList);
  }, []);
  return html`<div class="toasts" role="status" aria-live="polite"
    onMouseEnter=${() => hold(true)} onMouseLeave=${() => hold(false)}>
    ${list.map((t) => html`<${ToastItem} key=${t.id} t=${t} />`)}
  </div>`;
}
