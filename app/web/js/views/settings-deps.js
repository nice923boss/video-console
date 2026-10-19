// Settings page, dependencies: the outside programs the app runs (ffmpeg, Video2X) and the ComfyUI engine,
// with one-click installs into the data folder. The server downloads, checks and unpacks; progress
// arrives as "deps" events.
import { html, useEffect, useState } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { navigate } from "../lib/actions.js";
import { Button, Badge, Progress } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toastError } from "../components/toast.js";

const INFO = {
  ffmpeg: { name: "ffmpeg", icon: "film", text: "輸出影片、產生縮圖與燒錄字幕都靠它，沒有就無法輸出。" },
  video2x: { name: "Video2X", icon: "cpu", text: "選配。把 480P 影片放大到 1080P，需要支援 Vulkan 的顯示卡。" },
  comfyui: { name: "ComfyUI", icon: "server", text: "選配。本機生圖、生影片的引擎，需另外安裝，裝好後到「引擎」頁確認連線。" },
};
const COMFY_DOWNLOAD = "https://www.comfy.org/download";

const installing = (task) => !!task && (task.state === "downloading" || task.state === "unpacking");
const mb = (bytes) => Math.round(bytes / 1_000_000);

function badge(item) {
  if (installing(item.task)) return { tone: "info", label: "安裝中" };
  if (item.ready) return { tone: "ok", label: "就緒" };
  if (item.id === "comfyui") return { tone: "neutral", label: "未偵測" };
  return item.required ? { tone: "danger", label: "未就緒" } : { tone: "neutral", label: "選配未裝" };
}

async function loadDeps(redetect = false) {
  try {
    if (redetect) setState({ system: await api.post("/api/system/ffmpeg") });
    setState({ deps: await api.get("/api/system/deps") });
  } catch (err) {
    toastError(err, "無法讀取依賴元件狀態");
  }
}

async function install(ids) {
  try {
    for (const id of ids) setState({ deps: await api.post("/api/system/deps/install", { id }) });
  } catch (err) {
    toastError(err, "無法開始安裝");
  }
}

function TaskLine({ task }) {
  if (task.state === "downloading") {
    const pct = task.total ? task.received / task.total : 0;
    return html`<div class="dep-task">
      <${Progress} value=${pct} />
      <span class="dim">下載中 ${mb(task.received)} / ${mb(task.total)} MB（${Math.floor(pct * 100)}%）</span>
    </div>`;
  }
  if (task.state === "unpacking") {
    return html`<div class="dep-task"><${Progress} /><span class="dim">驗證完成，解壓縮中…</span></div>`;
  }
  if (task.state === "error") return html`<div class="dep-error">安裝失敗：${task.error}</div>`;
  return null;
}

function DepRow({ item }) {
  const info = INFO[item.id];
  const state = badge(item);
  const task = item.task;
  return html`<li class="dep-row">
    <span class="dep-icon"><${Icon} name=${info.icon} size=${18} /></span>
    <div class="dep-body">
      <div class="dep-head">
        <b>${info.name}</b>
        ${item.required && html`<span class="dim">必要</span>`}
        <${Badge} tone=${state.tone}>${state.label}<//>
        ${item.version && html`<span class="dim">${item.version}</span>`}
      </div>
      <span class="dim">${info.text}</span>
      ${item.ready && item.path && (item.id === "comfyui"
        ? html`<span class="dim">已連線：${item.path}</span>`
        : html`<code class="set-path">${item.path}</code>`)}
      ${task && html`<${TaskLine} task=${task} />`}
    </div>
    <div class="dep-actions">
      ${item.installable && !item.ready && html`<${Button} size="sm" icon="download" busy=${installing(task)}
          onClick=${() => install([item.id])}>${task && task.state === "error" ? "重試" : "安裝"}（約 ${item.sizeMB} MB）<//>`}
      ${item.id === "comfyui" && html`
        <a class="btn btn-sm" href=${COMFY_DOWNLOAD} target="_blank" rel="noopener noreferrer" title=${COMFY_DOWNLOAD}>
          <${Icon} name="external" size=${14} /><span class="btn-text">下載 ComfyUI</span>
        </a>
        <${Button} size="sm" icon="server" onClick=${() => navigate("engines", { enginesTab: "comfy" })}>到引擎頁<//>`}
    </div>
  </li>`;
}

export function DepsSection() {
  const deps = useStore((s) => s.deps);
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    loadDeps();
  }, []);
  const recheck = async () => {
    setChecking(true);
    await loadDeps(true);
    setChecking(false);
  };
  const items = (deps && deps.items) || [];
  const missing = items.filter((i) => i.required && i.installable && !i.ready && !installing(i.task));
  const busy = items.some((i) => installing(i.task));
  const message = !deps ? "檢查中…"
    : busy ? "安裝中，可以繼續使用其他功能；完成後狀態會自動更新。"
    : missing.length ? `缺少必要元件：${missing.map((i) => INFO[i.id].name).join("、")}。按「一鍵安裝」下載到資料資料夾，不需要系統管理員權限。`
    : "必要元件都已就緒。";
  return html`<div class="set-deps">
    <div class="dep-bar">
      <span class=${`dep-msg${missing.length ? " warn" : ""}`}>${message}</span>
      <${Button} size="sm" icon="refresh" busy=${checking} onClick=${recheck}>重新檢查<//>
      <${Button} size="sm" kind="primary" icon="download" disabled=${!missing.length}
        onClick=${() => install(missing.map((i) => i.id))}>一鍵安裝<//>
    </div>
    <ul class="dep-list">
      ${items.map((item) => html`<${DepRow} key=${item.id} item=${item} />`)}
    </ul>
    <p class="dim dep-foot">下載來源固定為 imageio-ffmpeg（PyPI）與 Video2X（GitHub 官方發行版），下載後先比對 SHA-256，
      相符才解壓縮到資料資料夾的 tools 子資料夾。</p>
  </div>`;
}
