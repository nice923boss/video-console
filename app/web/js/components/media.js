// Thumbnails with hover previews, and the full-screen lightbox for takes and media items.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useHotkeys } from "../lib/keys.js";
import { fileUrl, fmtDuration, fmtDateTime } from "../lib/format.js";
import { revealInFolder } from "../lib/actions.js";
import { Icon } from "./icons.js";
import { openModal } from "./overlay.js";
import { toast } from "./toast.js";

const STRIP_FRAMES = 8;
const HOVER_PLAY_MS = 250;
const KIND_ICON = { image: "image", video: "film", audio: "music" };

// Human wording for a take's `source` record.
export function sourceLabel(source) {
  if (!source || typeof source !== "object") return "";
  if (source.backend === "comfy") return `ComfyUI：${source.workflowName || source.workflow || "範本"}`;
  if (source.backend === "provider") return `雲端 API：${source.providerName || source.provider || ""}`;
  if (source.backend === "import") return source.name ? `匯入：${source.name}` : "匯入";
  if (source.backend === "local") {
    if (source.tool === "kenburns") return `運鏡動畫：${source.presetName || source.preset || ""}`;
    if (source.tool === "lastframe") return "擷取影片畫面";
    if (source.tool === "reverse") return "倒放";
  }
  return "";
}

// pid + path (project file) or src (any same-origin URL). strip: 8-frame sprite scrubbed by the
// pointer. video: project path played muted after a short hover. ratio: CSS aspect-ratio.
export function Thumb({ pid, path = "", src = "", strip = "", video = "", ratio = null, fit = "cover", icon = "image",
  alt = "", class: cls = "", children = null, ...rest }) {
  const url = src || (path ? fileUrl(pid, path) : "");
  const [failed, setFailed] = useState("");
  const [frame, setFrame] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);

  const enter = () => {
    if (!video) return;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setPlaying(true), HOVER_PLAY_MS);
  };
  const move = (e) => {
    if (!strip || video) return;
    const r = e.currentTarget.getBoundingClientRect();
    const i = Math.min(STRIP_FRAMES - 1, Math.max(0, Math.floor(((e.clientX - r.left) / r.width) * STRIP_FRAMES)));
    if (i !== frame) setFrame(i);
  };
  const leave = () => {
    clearTimeout(timer.current);
    setPlaying(false);
    setFrame(-1);
  };

  const broken = !url || failed === url;
  const style = [ratio ? `aspect-ratio:${ratio}` : "", rest.style || ""].filter(Boolean).join(";");
  const stripUrl = strip ? fileUrl(pid, strip) : "";
  return html`<div ...${rest} class=${`thumb thumb-${fit} ${cls}`} style=${style || undefined}
      onMouseEnter=${enter} onMouseMove=${move} onMouseLeave=${leave}>
    ${broken
      ? html`<div class="thumb-ph"><${Icon} name=${icon} size=${22} /></div>`
      : html`<img src=${url} alt=${alt} loading="lazy" decoding="async" draggable="false"
          onError=${() => setFailed(url)} />`}
    ${frame >= 0 && stripUrl && html`<div class="thumb-strip"
        style=${`background-image:url("${stripUrl}");background-position:${(frame / (STRIP_FRAMES - 1)) * 100}% 0`}>
      <i style=${`left:${(frame / STRIP_FRAMES) * 100}%;width:${100 / STRIP_FRAMES}%`}></i>
    </div>`}
    ${playing && video && html`<video class="thumb-video" src=${fileUrl(pid, video)} muted loop autoplay playsinline
        preload="auto"></video>`}
    ${children}
  </div>`;
}

// A take or media record as a lightbox entry.
export function mediaEntry(item, { name = "", actions = null } = {}) {
  return {
    id: item.id,
    kind: item.kind,
    file: item.file,
    thumb: item.thumb,
    name: name || item.label || item.name || "",
    width: item.width,
    height: item.height,
    duration: item.duration,
    fps: item.fps,
    hasAudio: item.hasAudio,
    created: item.created,
    prompt: item.prompt || "",
    negative: item.negative || "",
    seed: item.seed,
    source: sourceLabel(item.source),
    note: item.note || "",
    actions,
  };
}

function copyText(text, what) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(text).then(
    () => toast(`已複製${what}`, { duration: 1600 }),
    () => toast("無法存取剪貼簿", { level: "warn" }),
  );
}

function InfoRow({ label, children }) {
  return html`<div class="lb-row"><span>${label}</span><b>${children}</b></div>`;
}

function InfoPanel({ item }) {
  return html`<aside class="lb-info">
    ${item.name && html`<h3>${item.name}</h3>`}
    <div class="lb-rows">
      ${item.width && html`<${InfoRow} label="尺寸">${item.width} × ${item.height}<//>`}
      ${item.duration && item.kind !== "image" && html`<${InfoRow} label="長度">${fmtDuration(item.duration)}<//>`}
      ${item.fps && item.kind === "video" && html`<${InfoRow} label="影格率">${Math.round(item.fps * 100) / 100} fps<//>`}
      ${item.kind === "video" && html`<${InfoRow} label="聲音">${item.hasAudio ? "有" : "無"}<//>`}
      ${item.seed != null && html`<${InfoRow} label="種子">
        <button type="button" class="lb-copy mono" title="複製種子" onClick=${() => copyText(String(item.seed), "種子")}>
          ${item.seed}
        </button>
      <//>`}
      ${item.source && html`<${InfoRow} label="來源">${item.source}<//>`}
      ${item.created && html`<${InfoRow} label="建立">${fmtDateTime(item.created)}<//>`}
    </div>
    ${item.prompt && html`<div class="lb-block">
      <div class="lb-block-head">
        <span>提示詞</span>
        <button type="button" class="icon-btn" title="複製提示詞" onClick=${() => copyText(item.prompt, "提示詞")}>
          <${Icon} name="copy" size=${14} />
        </button>
      </div>
      <p>${item.prompt}</p>
    </div>`}
    ${item.negative && html`<div class="lb-block"><div class="lb-block-head"><span>負向提示詞</span></div>
      <p>${item.negative}</p></div>`}
    ${item.note && html`<div class="lb-block"><div class="lb-block-head"><span>備註</span></div><p>${item.note}</p></div>`}
  </aside>`;
}

function Stage({ pid, item }) {
  const url = fileUrl(pid, item.file);
  if (item.kind === "video") {
    return html`<video class="lb-media" src=${url} controls autoplay loop playsinline key=${item.id}></video>`;
  }
  if (item.kind === "audio") {
    return html`<div class="lb-audio" key=${item.id}>
      <${Icon} name="music" size=${56} />
      <audio src=${url} controls autoplay></audio>
    </div>`;
  }
  return html`<img class="lb-media" src=${url} alt=${item.name} key=${item.id} draggable="false" />`;
}

function Lightbox({ pid, items, start, close }) {
  const [index, setIndex] = useState(Math.min(Math.max(0, start), items.length - 1));
  const [info, setInfo] = useState(() => {
    try {
      return localStorage.getItem("vc.lightbox.info") !== "0";
    } catch {
      return true;
    }
  });
  const ref = useRef(null);
  const item = items[index];
  const go = (d) => setIndex((i) => (i + d + items.length) % items.length);
  const toggleInfo = () => setInfo((v) => {
    try {
      localStorage.setItem("vc.lightbox.info", v ? "0" : "1");
    } catch {
      // Storage can be unavailable; the panel still toggles for this session.
    }
    return !v;
  });

  useEffect(() => {
    const prev = document.activeElement;
    if (ref.current) ref.current.focus();
    return () => {
      if (prev && typeof prev.focus === "function" && document.contains(prev)) prev.focus();
    };
  }, []);

  useHotkeys({
    escape: { fn: () => close(), allowInInput: true },
    arrowleft: () => (items.length > 1 ? go(-1) : false),
    arrowright: () => (items.length > 1 ? go(1) : false),
    i: () => toggleInfo(),
  }, { modal: true });

  if (!item) return null;
  const download = fileUrl(pid, item.file, { download: true, name: item.name ? `${item.name}${extOf(item.file)}` : "" });
  return html`<div class="lightbox" role="dialog" aria-modal="true" aria-label=${item.name || "預覽"} tabindex="-1"
      ref=${ref} onMouseDown=${(e) => { if (e.target === e.currentTarget) close(); }}>
    <header class="lb-top">
      <span class="lb-kind"><${Icon} name=${KIND_ICON[item.kind] || "file"} size=${16} /></span>
      <span class="lb-title">${item.name || "未命名"}</span>
      ${items.length > 1 && html`<span class="lb-count mono">${index + 1} / ${items.length}</span>`}
      <span class="lb-spacer"></span>
      ${(item.actions || []).map((a) => html`<button type="button" class="btn btn-sm btn-ghost lb-act" key=${a.label}
          onClick=${() => a.fn(item)}>${a.icon && html`<${Icon} name=${a.icon} size=${14} />`}<span>${a.label}</span></button>`)}
      <button type="button" class=${`icon-btn ${info ? "active" : ""}`} title="資訊（I）" aria-label="資訊"
        onClick=${toggleInfo}><${Icon} name="info" /></button>
      <a class="icon-btn" href=${download} download title="下載" aria-label="下載"><${Icon} name="download" /></a>
      <button type="button" class="icon-btn" title="在檔案總管中顯示" aria-label="在檔案總管中顯示"
        onClick=${() => revealInFolder(item.file)}><${Icon} name="folder-open" /></button>
      <button type="button" class="icon-btn" title="關閉（Esc）" aria-label="關閉" onClick=${() => close()}>
        <${Icon} name="x" />
      </button>
    </header>
    <div class=${`lb-main ${info ? "with-info" : ""}`}>
      <div class="lb-stage" onMouseDown=${(e) => { if (e.target === e.currentTarget) close(); }}>
        <${Stage} pid=${pid} item=${item} />
        ${items.length > 1 && html`
          <button type="button" class="lb-nav prev" title="上一個（←）" aria-label="上一個" onClick=${() => go(-1)}>
            <${Icon} name="chevron-left" size=${26} />
          </button>
          <button type="button" class="lb-nav next" title="下一個（→）" aria-label="下一個" onClick=${() => go(1)}>
            <${Icon} name="chevron-right" size=${26} />
          </button>`}
      </div>
      ${info && html`<${InfoPanel} item=${item} />`}
    </div>
  </div>`;
}

function extOf(path) {
  const m = /\.[a-z0-9]{2,5}$/i.exec(path || "");
  return m ? m[0] : "";
}

// items: mediaEntry() records. Resolves when the lightbox closes.
export function openLightbox(pid, items, start = 0) {
  const list = items.filter((x) => x && x.file);
  if (!list.length) return Promise.resolve();
  const at = Math.max(0, list.indexOf(items[start]));
  return openModal((close) => html`<${Lightbox} pid=${pid} items=${list} start=${at} close=${close} />`);
}
