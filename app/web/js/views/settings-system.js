// Settings page, video output and system: where ffmpeg comes from, the default export choices, app
// facts for support, the data folders, the debug bundle and a clean shutdown.
import { html, useState } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveSettings, revealSystemFolder, pickSystemFolder } from "../lib/actions.js";
import { stopEvents } from "../lib/events.js";
import { fmtBytes, fmtDateTime } from "../lib/format.js";
import { Button, Field, TextInput, NumberInput, Select, Segmented, Toggle } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { confirmDialog } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { useSectionDraft, DraftFoot } from "./settings-draft.js";
import { defaultExportFolder } from "../components/export-folder.js";
import { openRelease } from "../lib/release.js";

const ENCODERS = [
  { value: "auto", label: "自動偵測顯示卡（NVIDIA、AMD、Intel，都沒有就用 CPU）" },
  { value: "libx264", label: "CPU：libx264（相容性最高）" },
  { value: "h264_nvenc", label: "NVIDIA 顯示卡：NVENC" },
  { value: "h264_amf", label: "AMD 顯示卡：AMF" },
  { value: "h264_qsv", label: "Intel 內顯：Quick Sync" },
];
const QUALITY = [
  { value: "high", label: "高畫質", title: "檔案較大，編碼較慢，適合正式交件" },
  { value: "standard", label: "標準", title: "畫質與檔案大小的折衷" },
  { value: "draft", label: "草稿", title: "編碼最快，適合先看節奏" },
];
const FOLDERS = [
  { what: "data", label: "資料資料夾", icon: "folder-open", title: "專案、設定與加密金鑰都在這裡" },
  { what: "logs", label: "記錄檔", icon: "file", title: "app.log 是目前的記錄，app.log.1～3 是較舊的" },
  { what: "workflows", label: "工作流程範本", icon: "workflow", title: "ComfyUI 範本的 JSON 檔" },
];

const sameFile = (a, b) => a.replace(/\//g, "\\").toLowerCase() === b.replace(/\//g, "\\").toLowerCase();

async function refreshSystem(path) {
  try {
    setState({ system: await api.post(path) });
    return true;
  } catch (err) {
    toastError(err, "無法讀取 ffmpeg 狀態");
    return false;
  }
}

function FfmpegStatus({ ffmpeg, configured }) {
  const [busy, setBusy] = useState(false);
  const redetect = async () => {
    setBusy(true);
    if (await refreshSystem("/api/system/ffmpeg")) toast("已重新偵測 ffmpeg");
    setBusy(false);
  };
  const fellBack = ffmpeg.available && configured && !sameFile(configured, ffmpeg.path);
  const tone = !ffmpeg.available ? "danger" : fellBack ? "warn" : "ok";
  return html`<div class=${`note note-${tone} set-ffmpeg`} role="status">
    <${Icon} name=${tone === "ok" ? "check-circle" : "alert"} size=${15} />
    <span class="note-text">
      ${ffmpeg.available
        ? html`<b>ffmpeg ${ffmpeg.version || "版本不明"}</b><code class="set-path">${ffmpeg.path}</code>
            ${fellBack && html`<span>指定的檔案找不到，目前改用自動找到的 ffmpeg。</span>`}`
        : html`<b>找不到 ffmpeg</b><span>無法輸出影片與產生縮圖。請到上方「依賴元件」按「一鍵安裝」，或在下方指定 ffmpeg.exe 的完整路徑。</span>`}
    </span>
    <${Button} size="sm" icon="refresh" busy=${busy} onClick=${redetect}>重新偵測<//>
  </div>`;
}

export function OutputSection() {
  const ffmpegCfg = useStore((s) => s.settings.ffmpeg);
  const exportCfg = useStore((s) => s.settings.export);
  const ffmpeg = useStore((s) => (s.system && s.system.ffmpeg) || { available: false, path: "", version: "" });
  const defaultFolder = useStore((s) => defaultExportFolder(s.system && s.system.dataDir));
  const saved = { path: ffmpegCfg.path || "", encoder: ffmpegCfg.encoder || "auto", quality: exportCfg.quality,
    burnCaptions: !!exportCfg.burnCaptions, captionSize: exportCfg.captionSize, folder: exportCfg.folder || "" };
  const form = useSectionDraft(saved, (d) => ({
    ffmpeg: { path: d.path.trim().replace(/^"(.*)"$/, "$1"), encoder: d.encoder },
    export: { quality: d.quality, burnCaptions: d.burnCaptions, captionSize: d.captionSize,
      folder: d.folder.trim().replace(/^"(.*)"$/, "$1") },
  }), "已儲存影片輸出設定");
  const { draft, set } = form;
  const pathError = draft.path.trim() && !/\.exe"?$/i.test(draft.path.trim()) ? "請填 ffmpeg.exe 的完整路徑" : "";
  const folder = draft.folder.trim().replace(/^"(.*)"$/, "$1");
  const folderError = folder && !/^([a-zA-Z]:[\\/]|\\\\)/.test(folder) ? "請填完整路徑，例如 D:\\影片輸出" : "";
  const [picking, setPicking] = useState(false);
  const browse = async () => {
    setPicking(true);
    const picked = await pickSystemFolder(folder || defaultFolder, "選擇匯出資料夾");
    setPicking(false);
    if (picked) set({ folder: picked });
  };
  const save = async () => {
    const pathChanged = draft.path.trim() !== saved.path;
    if (await form.save() && pathChanged) await refreshSystem("/api/system/ffmpeg");
  };
  return html`<div class="set-output">
    <${FfmpegStatus} ffmpeg=${ffmpeg} configured=${ffmpegCfg.path || ""} />
    <${Field} label="ffmpeg.exe 位置" error=${pathError}
        hint="留白會自動尋找：系統 PATH、imageio-ffmpeg 套件、「依賴元件」安裝的版本、WinGet 與 Scoop 的安裝位置">
      <${TextInput} mono value=${draft.path} spellcheck=${false} placeholder="C:\\ffmpeg\\bin\\ffmpeg.exe"
        onChange=${(path) => set({ path })} />
    <//>
    <${Field} label="編碼器" hint="輸出對話框選「自動」時用這裡的設定；硬體編碼器失敗時會自動改用 CPU 重新輸出">
      <${Select} value=${draft.encoder} options=${ENCODERS} onChange=${(encoder) => set({ encoder })} />
    <//>
    <div class="row-2">
      <${Field} label="預設畫質">
        <${Segmented} value=${draft.quality} options=${QUALITY} onChange=${(quality) => set({ quality })} />
      <//>
      <${Field} label="內建字幕大小" hint="沒有套用字幕樣式的字幕使用，預覽與輸出都照這個大小，以 1080 高的畫面為準">
        <${NumberInput} value=${draft.captionSize} min=${16} max=${160} step=${2} suffix="px"
          onCommit=${(captionSize) => set({ captionSize })} />
      <//>
    </div>
    <${Toggle} checked=${draft.burnCaptions} onChange=${(burnCaptions) => set({ burnCaptions })} label="預設把字幕燒進畫面"
      hint="關閉時影片不含字幕，SRT 字幕檔可在輸出對話框另外下載" />
    <${Field} label="匯出資料夾" error=${folderError}
        hint=${`所有專案輸出的影片、音訊與 ZIP 都存到這裡，留白使用預設位置 ${defaultFolder}。改了只影響之後的輸出，舊檔案留在原處，仍列在各專案的輸出成品`}
        after=${html`<${Button} size="sm" kind="ghost" icon="folder" busy=${picking}
            title="用 Windows 的選擇資料夾視窗挑選，選好後按下方「儲存」" onClick=${browse}>選擇<//>
          <${Button} size="sm" kind="ghost" icon="folder-open" title="開啟目前儲存的匯出資料夾"
            onClick=${() => revealSystemFolder("exports")}>開啟<//>`}>
      <${TextInput} mono value=${draft.folder} spellcheck=${false} placeholder=${defaultFolder}
        onChange=${(folder) => set({ folder })} />
    <//>
    <${DraftFoot} form=${form} invalid=${!!pathError || !!folderError} onSave=${save} />
  </div>`;
}

function SystemFacts({ system, version }) {
  const rows = [
    ["版本", version || system.version],
    ["Python", system.python],
    ["作業系統", system.platform],
    ["伺服器程序", system.pid ? `PID ${system.pid}` : ""],
    ["啟動時間", system.started ? fmtDateTime(system.started) : ""],
  ].filter(([, v]) => v);
  return html`<dl class="kv set-facts">
    ${rows.map(([k, v]) => html`<div key=${k}><dt>${k}</dt><dd>${v}</dd></div>`)}
    <div><dt>資料位置</dt><dd><code class="set-path">${system.dataDir}</code></dd></div>
  </dl>`;
}

// IN-04: the latest release on GitHub. The startup check announces a newer one once; this asks again any time.
function ReleaseCheck() {
  const [info, setInfo] = useState(null);
  const [busy, setBusy] = useState(false);
  const check = async () => {
    setBusy(true);
    try {
      setInfo(await api.post("/api/system/release/check"));
    } catch (err) {
      toastError(err, "無法檢查更新");
    }
    setBusy(false);
  };
  const status = !info ? ""
    : info.remark || (info.newer ? `GitHub 上已有 ${info.latest}${info.published ? `（${info.published} 發布）` : ""}`
      : `已是最新版（GitHub 上最新是 ${info.latest}）`);
  return html`<${Field} label="更新" hint="啟動時會比對 GitHub 上的最新版本，有新版時提示一次。更新要到下載頁下載新版，資料資料夾不受影響">
    <div class="set-buttons">
      <${Button} size="sm" icon="refresh" busy=${busy} onClick=${check}>檢查更新<//>
      ${info && info.newer && html`<${Button} size="sm" kind="primary" icon="external"
        onClick=${() => openRelease(info)}>前往下載頁<//>`}
      ${status && html`<span class=${info.remark ? "warn-text" : "dim"}>${status}</span>`}
    </div>
  <//>`;
}

// QA-09: one zip for a bug report, and a way to start the logs over.
function DebugData({ logBytes }) {
  const projects = useStore((s) => s.projects || []);
  const current = useStore((s) => s.pid || "");
  const [pid, setPid] = useState(current);
  const [clearing, setClearing] = useState(false);
  const options = [{ value: "", label: "不附專案" }, ...projects.map((p) => ({ value: p.id, label: `附上「${p.name}」` }))];
  const download = () => {
    const a = document.createElement("a");
    a.href = `/api/system/debug-bundle${pid ? `?project=${encodeURIComponent(pid)}` : ""}`;
    a.click(); // the server answers with an attachment, so the window stays on this page
  };
  const clear = async () => {
    const ok = await confirmDialog({
      title: "清除記錄檔？",
      message: `刪除 app.log 與較舊的記錄（共 ${fmtBytes(logBytes || 0)}）。清除後之前的錯誤細節就查不到了，要回報問題請先打包。`,
      confirmLabel: "清除",
      danger: true,
    });
    if (!ok) return;
    setClearing(true);
    try {
      const res = await api.post("/api/system/clear-logs");
      setState({ system: res.system });
      toast(`已清除記錄檔（${fmtBytes(res.freed)}）`);
    } catch (err) {
      toastError(err, "無法清除記錄檔");
    }
    setClearing(false);
  };
  return html`<${Field} label="除錯資料"
      hint="把記錄檔、版本資訊、設定、工作清單（可加上一個專案的內容）打包成 ZIP，回報問題時附上。不含 API 金鑰，只標示有沒有設定；ntfy 主題也會遮住">
    <div class="set-buttons">
      <${Select} value=${pid} options=${options} onChange=${setPid} />
      <${Button} size="sm" icon="download" onClick=${download}>打包下載<//>
      <${Button} size="sm" kind="ghost" icon="trash" busy=${clearing} onClick=${clear}
        title="清除 app.log 與較舊的記錄檔">清除記錄檔${logBytes ? `（${fmtBytes(logBytes)}）` : ""}<//>
    </div>
  <//>`;
}

async function shutdown(jobs) {
  const running = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  const ok = await confirmDialog({
    title: "結束 Video Console？",
    message: running
      ? `還有 ${running} 個工作在執行或排隊。結束後執行中的工作會中斷。下次啟動時，ComfyUI 與本機工作（輸出影片等）會接續或重新排隊；雲端工作在服務支援查詢時接續等結果，否則標為中斷，可以再按重試。排隊中的工作會保留。`
      : "背景伺服器會停止，之後可以直接關閉這個視窗。下次執行 Video Console.exe（或原始碼版的「啟動 Video Console.bat」）開啟。",
    confirmLabel: "結束程式",
    danger: running > 0,
  });
  if (!ok) return;
  try {
    await api.post("/api/shutdown");
  } catch (err) {
    toastError(err, "無法結束程式");
    return;
  }
  stopEvents();
  setState({ boot: "closed" });
}

export function SystemSection() {
  const system = useStore((s) => s.system);
  const version = useStore((s) => s.version);
  const jobs = useStore((s) => s.jobs || []);
  const autoExit = useStore((s) => s.settings.ui.autoExit !== false);
  return html`<div class="set-system">
    ${system ? html`<${SystemFacts} system=${system} version=${version} />`
      : html`<p class="dim">讀不到系統資訊，重新整理頁面再試一次。</p>`}
    <div class="set-buttons">
      ${FOLDERS.map((f) => html`<${Button} key=${f.what} size="sm" icon=${f.icon} title=${f.title}
        onClick=${() => revealSystemFolder(f.what)}>${f.label}<//>`)}
    </div>
    <${ReleaseCheck} />
    <${DebugData} logBytes=${system && system.logBytes} />
    <hr class="set-sep" />
    <${Toggle} checked=${autoExit} onChange=${(autoExit) => saveSettings({ ui: { autoExit } })} label="背景啟動時自動結束"
      hint="由 Claude 或工具在背景啟動（沒有托盤圖示）時，所有視窗都關閉兩分鐘、而且佇列沒有工作，背景伺服器自動結束；關閉這個選項，伺服器會一直在背景執行。從桌面啟動的 Video Console 關閉視窗後會留在系統托盤，要從托盤圖示的右鍵選單結束" />
    <div class="set-danger">
      <div>
        <b>結束程式</b>
        <span class="dim">停止背景伺服器。排隊中的工作會保留，下次啟動後繼續。</span>
      </div>
      <${Button} kind="danger" icon="power" onClick=${() => shutdown(jobs)}>結束程式<//>
    </div>
  </div>`;
}
