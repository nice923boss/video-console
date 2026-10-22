// Export dialog: render the sequence to one MP4 (quality, encoder, captions, fades, clip audio, framing)
// or zip the project for hand-off. Quality and caption choices can be kept as the app defaults.
import { html, useState, useMemo } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { saveSettings, updateProject, navigate } from "../lib/actions.js";
import { submitJob } from "../lib/jobs-actions.js";
import { buildSequence, sequenceDuration } from "../lib/sequence.js";
import { fmtDuration } from "../lib/format.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, Select, Segmented, Toggle, NumberInput, Tabs } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast } from "../components/toast.js";

const QUALITY = [
  { value: "high", label: "高畫質", title: "檔案較大，編碼較慢，適合正式交件" },
  { value: "standard", label: "標準", title: "畫質與檔案大小的折衷" },
  { value: "draft", label: "草稿", title: "編碼最快，適合先看節奏" },
];
const ENCODERS = [
  { value: "auto", label: "自動（依設定頁，或自動偵測顯示卡）" },
  { value: "libx264", label: "CPU：libx264（相容性最高）" },
  { value: "h264_nvenc", label: "NVIDIA 顯示卡：NVENC" },
  { value: "h264_amf", label: "AMD 顯示卡：AMF" },
  { value: "h264_qsv", label: "Intel 內顯：Quick Sync" },
];
const FIT = [
  { value: "contain", label: "完整顯示", title: "畫面比例不同時補黑邊" },
  { value: "cover", label: "填滿畫面", title: "畫面比例不同時裁掉多出的部分" },
];
const MODES = [
  { id: "video", label: "輸出影片", icon: "film" },
  { id: "package", label: "打包專案", icon: "package" },
];

function queued(job) {
  toast(`已加入佇列：${job.title}`, {
    action: { label: "查看進度", fn: () => navigate("library", { libraryTab: "exports" }) },
  });
}

function Summary({ project, clips }) {
  const s = project.settings;
  return html`<div class="exp-summary">
    <div><span>鏡頭</span><b>${clips.length}</b></div>
    <div><span>長度</span><b>${fmtDuration(sequenceDuration(clips))}</b></div>
    <div><span>尺寸</span><b>${s.width} × ${s.height}</b></div>
    <div><span>影格率</span><b>${s.fps} fps</b></div>
    <div><span>字幕</span><b>${(project.captions || []).length}</b></div>
    <div><span>音軌</span><b>${(project.audio || []).length}</b></div>
  </div>`;
}

// Each form is a hook returning { body, footer } so both keep their state while the user flips tabs.
function useVideoForm(project, clips, close) {
  const { settings, system } = getState();
  const defaults = (settings && settings.export) || {};
  const ffmpegOk = !!(system && system.ffmpeg && system.ffmpeg.available);
  const [quality, setQuality] = useState(defaults.quality || "high");
  const [encoder, setEncoder] = useState("auto");
  const [burn, setBurn] = useState(!!defaults.burnCaptions);
  const [captionSize, setCaptionSize] = useState(defaults.captionSize || 52);
  const [fadeIn, setFadeIn] = useState(project.settings.fadeIn || 0);
  const [fadeOut, setFadeOut] = useState(project.settings.fadeOut || 0);
  const [clipAudio, setClipAudio] = useState(true);
  const [fit, setFit] = useState("contain");
  const [keep, setKeep] = useState(false);
  const [busy, setBusy] = useState(false);
  const hasCaptions = (project.captions || []).length > 0;
  const noClips = "序列裡沒有任何鏡頭，請確認鏡頭有勾選「放入序列」";
  const blocked = !clips.length ? noClips : !ffmpegOk ? "找不到 ffmpeg" : "";

  const submit = async () => {
    setBusy(true);
    const s = project.settings;
    if ((s.fadeIn || 0) !== fadeIn || (s.fadeOut || 0) !== fadeOut) {
      await updateProject({ settings: { fadeIn, fadeOut } }, { label: "修改淡入淡出" });
    }
    if (keep) await saveSettings({ export: { quality, burnCaptions: burn, captionSize } });
    const job = await submitJob("export", null, {
      options: { quality, encoder, burnCaptions: burn, captionSize, fadeIn, fadeOut, clipAudio, fit },
    }, { quiet: true });
    setBusy(false);
    if (!job) return;
    queued(job);
    close(job);
  };

  const footer = html`
    <span class="modal-foot-note dim"><${Icon} name="folder" size=${14} />存到專案的 exports 資料夾</span>
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="film" busy=${busy} disabled=${!!blocked} title=${blocked || undefined}
      onClick=${submit}>開始輸出<//>`;

  const body = html`
    <${Summary} project=${project} clips=${clips} />
    ${!ffmpegOk && html`<div class="note note-warn">
      <${Icon} name="alert" size=${15} />
      <span class="note-text">找不到 ffmpeg，無法輸出影片。請到設定頁的「依賴元件」按「一鍵安裝」，或指定 ffmpeg.exe 位置後重新偵測。</span>
      <${Button} size="sm" onClick=${() => { close(null); navigate("settings"); }}>前往設定<//>
    </div>`}
    ${!clips.length && html`<div class="note note-warn">
      <${Icon} name="alert" size=${15} /><span class="note-text">${noClips}</span>
    </div>`}
    <div class="row-2">
      <${Field} label="畫質">
        <${Segmented} value=${quality} onChange=${setQuality} options=${QUALITY} />
      <//>
      <${Field} label="畫面比例不同時">
        <${Segmented} value=${fit} onChange=${setFit} options=${FIT} />
      <//>
    </div>
    <${Field} label="編碼器" hint="選擇的硬體編碼器無法使用時，會自動改用 CPU 重新輸出">
      <${Select} value=${encoder} onChange=${setEncoder} options=${ENCODERS} />
    <//>
    <div class="row-2">
      <${Field} label="開頭淡入">
        <${NumberInput} value=${fadeIn} onCommit=${setFadeIn} min=${0} max=${10} step=${0.1} digits=${1} suffix="秒" />
      <//>
      <${Field} label="結尾淡出">
        <${NumberInput} value=${fadeOut} onCommit=${setFadeOut} min=${0} max=${10} step=${0.1} digits=${1} suffix="秒" />
      <//>
    </div>
    <${Toggle} checked=${burn} onChange=${setBurn} label="把字幕燒進畫面"
      hint=${hasCaptions ? "關掉時影片不含字幕，可以另外下載 SRT 字幕檔" : "這個專案還沒有字幕，打開也不會有作用"} />
    ${burn && html`<${Field} label="內建字幕大小" inline hint="沒有套用字幕樣式的字幕使用；外框、陰影等樣式在字幕屬性的「編輯樣式」設定">
      <${NumberInput} value=${captionSize} onCommit=${setCaptionSize} min=${16} max=${160} step=${2} suffix="px" />
    <//>`}
    <${Toggle} checked=${clipAudio} onChange=${setClipAudio} label="保留影片片段本身的聲音"
      hint="關掉時只會有音軌上的音樂與旁白" />
    <${Toggle} checked=${keep} onChange=${setKeep} label="把畫質與字幕設定存成之後的預設" />`;
  return { body, footer };
}

function usePackageForm(project, close, fullBackup) {
  const [videos, setVideos] = useState(true);
  const [latest, setLatest] = useState(true);
  const [full, setFull] = useState(fullBackup);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    const job = await submitJob("package", null, { includeVideos: videos || full, includeExport: latest, fullBackup: full },
      { quiet: true });
    setBusy(false);
    if (!job) return;
    queued(job);
    close(job);
  };
  const footer = html`
    <span class="modal-foot-note dim"><${Icon} name="folder" size=${14} />ZIP 檔存到專案的 exports 資料夾</span>
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="package" busy=${busy} disabled=${!project.shots.length} onClick=${submit}>開始打包<//>`;
  const body = html`
    <p class="dim exp-lede">把專案整理成一個 ZIP，方便交給剪輯師、客戶或另一台電腦。打包檔一定會包含：</p>
    <ul class="exp-contents">
      <li><${Icon} name="grid" size=${15} />分鏡表（網頁，圖片已附在包內）</li>
      <li><${Icon} name="list" size=${15} />鏡頭清單 CSV 與字幕 SRT</li>
      <li><${Icon} name="image" size=${15} />每個鏡頭目前選用的圖片</li>
      <li><${Icon} name="music" size=${15} />音軌用到的音樂與旁白</li>
    </ul>
    <${Toggle} checked=${full} onChange=${setFull} label="完整備份（可匯入還原）"
      hint="包含所有版本、縮圖與素材，可用「匯入專案」還原到另一台電腦" />
    <${Toggle} checked=${videos || full} disabled=${full} onChange=${setVideos} label="包含每個鏡頭選用的影片"
      hint=${full ? "完整備份一定包含所有影片" : "影片通常占打包檔大部分的容量，只需要分鏡時可以關掉"} />
    <${Toggle} checked=${latest} onChange=${setLatest} label="附上最新一次輸出的完整影片" />`;
  return { body, footer };
}

function ExportBody({ project, initial, fullBackup, close }) {
  const [mode, setMode] = useState(initial);
  const clips = useMemo(() => buildSequence(project), [project]);
  const video = useVideoForm(project, clips, close);
  const pack = usePackageForm(project, close, fullBackup);
  const form = mode === "package" ? pack : video;
  return html`<${Modal} title="輸出" width=${600} backdropClose=${false} onClose=${() => close(null)} footer=${form.footer} class="export-dialog">
    <${Tabs} class="exp-modes" value=${mode} tabs=${MODES} onChange=${setMode} />
    ${form.body}
  <//>`;
}

// mode: "video" or "package"; fullBackup starts the package as a restorable backup (匯出專案).
// Resolves with the submitted job, or null when canceled.
export function openExportDialog(mode = "video", { fullBackup = false } = {}) {
  const project = getState().project;
  if (!project) {
    toast("請先開啟專案", { level: "warn" });
    return Promise.resolve(null);
  }
  const initial = mode === "package" ? "package" : "video";
  return openModal((close) => html`<${ExportBody} project=${project} initial=${initial} fullBackup=${fullBackup} close=${close} />`);
}
