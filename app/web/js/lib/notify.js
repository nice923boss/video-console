// Job completion notices: an in-app toast, a desktop notification while the window is hidden, and a
// short chime. Notices that arrive close together (a batch finishing) are grouped into one.
import { getState } from "./store.js";
import { toast } from "../components/toast.js";
import { revealJob } from "./actions.js";

const GROUP_MS = 700;
let pending = [];
let flushTimer = null;
let audioCtx = null;

function ensureAudio() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    audioCtx = new Ctx();
  }
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}

// Browsers only allow sound after a user gesture, so unlock audio on the first click.
window.addEventListener("pointerdown", () => ensureAudio(), { once: true, capture: true });

export function chime(ok = true) {
  const ctx = ensureAudio();
  if (!ctx || ctx.state !== "running") return;
  const notes = ok ? [880, 1318.5] : [392, 261.6];
  const t0 = ctx.currentTime + 0.01;
  notes.forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = ok ? "sine" : "triangle";
    osc.frequency.value = freq;
    const start = t0 + i * 0.13;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.1, start + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.32);
    osc.connect(gain).connect(ctx.destination);
    osc.start(start);
    osc.stop(start + 0.36);
  });
}

// Called from the settings page (a user gesture) when desktop notices are switched on.
export async function requestDesktopPermission() {
  if (!("Notification" in window)) return "unsupported";
  if (Notification.permission !== "default") return Notification.permission;
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}

function desktopNotice(title, body, jobId) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, { body, tag: jobId || "vc", icon: "/icon.svg" });
    n.onclick = () => {
      window.focus();
      if (jobId) revealJob(jobId);
      n.close();
    };
  } catch {
    // Some shells (or a revoked permission) refuse notifications; the toast already covers it.
  }
}

function flush() {
  flushTimer = null;
  const items = pending;
  pending = [];
  if (!items.length) return;
  const settings = getState().settings || {};
  const prefs = settings.notify || {};
  const failed = items.filter((n) => n.level === "error");
  const ok = items.length - failed.length;

  if (items.length === 1) {
    const n = items[0];
    toast(n.message || n.title, {
      level: n.level === "error" ? "error" : "success",
      title: n.title,
      action: n.jobId ? { label: "查看", fn: () => revealJob(n.jobId) } : null,
    });
  } else {
    const parts = [];
    if (ok) parts.push(`${ok} 個完成`);
    if (failed.length) parts.push(`${failed.length} 個失敗`);
    const target = (failed[0] || items[items.length - 1]).jobId;
    toast(failed.length ? `失敗：${failed.map((n) => n.title).slice(0, 3).join("、")}` : "可以到版本或素材查看結果", {
      level: failed.length ? "error" : "success",
      title: `工作結束：${parts.join("，")}`,
      action: target ? { label: "查看", fn: () => revealJob(target) } : null,
    });
  }

  if (prefs.sound !== false) chime(!failed.length);
  if (prefs.desktop !== false && document.hidden) {
    if (items.length === 1) desktopNotice(items[0].title, items[0].message, items[0].jobId);
    else desktopNotice("Video Console", `${ok} 個工作完成${failed.length ? `，${failed.length} 個失敗` : ""}`, items[0].jobId);
  }
}

export function handleNotify(data) {
  pending.push(data);
  clearTimeout(flushTimer);
  flushTimer = setTimeout(flush, GROUP_MS);
}
