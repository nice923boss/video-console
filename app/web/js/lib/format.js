// Formatting and small pure helpers.

const pad = (n, w = 2) => String(Math.floor(n)).padStart(w, "0");

// 65.24 -> "01:05.2"; an hour or more -> "1:02:03"
export function fmtTime(sec, { tenths = true } = {}) {
  const s = Math.max(0, Number(sec) || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = s % 60;
  if (h) return `${h}:${pad(m)}:${pad(rest)}`;
  if (!tenths) return `${pad(m)}:${pad(rest)}`;
  const whole = Math.floor(rest);
  const tenth = Math.floor((rest - whole) * 10 + 1e-6);
  return `${pad(m)}:${pad(whole)}.${tenth}`;
}

export function fmtDuration(sec) {
  const s = Math.max(0, Number(sec) || 0);
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} 秒`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`;
  return `${Math.floor(m / 60)} 小時 ${m % 60} 分`;
}

export function fmtSeconds(sec, digits = 1) {
  return `${(Number(sec) || 0).toFixed(digits)}s`;
}

export function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let x = v / 1024;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  return `${x < 10 ? x.toFixed(1) : Math.round(x)} ${units[i]}`;
}

const toMs = (ts) => (ts > 1e12 ? ts : ts * 1000);

export function fmtRelTime(ts) {
  if (!ts) return "";
  const diff = (Date.now() - toMs(ts)) / 1000;
  if (diff < 45) return "剛剛";
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))} 分鐘前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小時前`;
  const d = new Date(toMs(ts));
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (d >= yesterday) return "昨天";
  if (diff < 7 * 86400) return `${Math.round(diff / 86400)} 天前`;
  return fmtDate(ts);
}

export function fmtDate(ts) {
  const d = new Date(toMs(ts));
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

export function fmtDateTime(ts) {
  if (!ts) return "";
  const d = new Date(toMs(ts));
  return `${fmtDate(ts)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtClock(ts) {
  const d = new Date(toMs(ts));
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const ID_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz";

// Matches the server rule ^[a-z]{1,3}_[a-z0-9]{6,24}$
export function newId(prefix) {
  const bytes = new Uint8Array(10);
  crypto.getRandomValues(bytes);
  let out = "";
  for (const b of bytes) out += ID_CHARS[b % ID_CHARS.length];
  return `${prefix}_${out}`;
}

export function randomSeed() {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return buf[0];
}

export function debounce(fn, ms) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function fileUrl(pid, path, { download = false, name = "" } = {}) {
  if (!pid || !path) return "";
  let url = `/api/projects/${encodeURIComponent(pid)}/file?path=${encodeURIComponent(path)}`;
  if (download) url += `&download=1${name ? `&name=${encodeURIComponent(name)}` : ""}`;
  return url;
}

// Same wording as the server's shot_label so toasts and job titles match.
export function shotLabel(shot, index) {
  const name = ((shot && shot.name) || "").trim();
  return name ? `#${index + 1} ${name}` : `鏡頭 ${index + 1}`;
}

export function shotName(shot, index) {
  const name = ((shot && shot.name) || "").trim();
  return name || `鏡頭 ${index + 1}`;
}

export function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

export function round(v, digits = 2) {
  const f = 10 ** digits;
  return Math.round(v * f) / f;
}

const CJK_RE = /\p{Script=Han}/u;

export function hasCJK(text) {
  return CJK_RE.test(text || "");
}

const EXT = {
  image: [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"],
  video: [".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"],
  audio: [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"],
};

export const ACCEPT_ALL = [...EXT.image, ...EXT.video, ...EXT.audio].join(",");
export const ACCEPT_VISUAL = [...EXT.image, ...EXT.video].join(",");
export const ACCEPT_IMAGE = EXT.image.join(",");
export const ACCEPT_AUDIO = EXT.audio.join(",");

export function kindOfName(name) {
  const lower = (name || "").toLowerCase();
  const dot = lower.lastIndexOf(".");
  const ext = dot >= 0 ? lower.slice(dot) : "";
  for (const [kind, list] of Object.entries(EXT)) if (list.includes(ext)) return kind;
  return null;
}

export const KIND_LABEL = {
  image: "生圖", video: "生影片", upscale: "放大", upscalevideo: "影片放大", kenburns: "運鏡動畫", lastframe: "取末格",
  ingest: "匯入", export: "輸出影片", package: "打包", voice: "配音",
  transcribe: "語音轉字幕",
};

export const STATUS_LABEL = {
  queued: "排隊中", running: "執行中", done: "完成", failed: "失敗", canceled: "已取消", interrupted: "已中斷",
};

export function plainNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export function sortByOrder(list) {
  return [...list].sort((a, b) => (a.order ?? a.created ?? 0) - (b.order ?? b.created ?? 0));
}
