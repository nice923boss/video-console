// Export dialog: render the sequence to one MP4 (platform preset, quality, encoder, captions, fades, clip audio,
// framing) after a pre-export check and with a time estimate, render only its sound to WAV / MP3, or zip the project for hand-off. Quality and caption choices can be kept as the app defaults.
import { html, useState, useMemo } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { saveSettings, updateProject, navigate } from "../lib/actions.js";
import { submitJob } from "../lib/jobs-actions.js";
import { buildSequence, sequenceDuration } from "../lib/sequence.js";
import { fmtDuration } from "../lib/format.js";
import { PRESETS, RESOLUTIONS, presetOutput, resolutionSize, aspectDiffers, estimateExport } from "../lib/export-presets.js";
import { copyChapters } from "../lib/chapters.js";
import { ExportCheck } from "./export-check.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, Select, Segmented, Toggle, NumberInput, Tabs } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { ExportFolderNote } from "../components/export-folder.js";
import { toast } from "../components/toast.js";

export const QUALITY = [
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
  { id: "audio", label: "輸出音訊", icon: "music" },
  { id: "package", label: "打包專案", icon: "package" },
];
const AUDIO_FORMATS = [
  { value: "wav", label: "WAV", title: "無損 16-bit 48kHz，給配音師或後製用" },
  { value: "mp3", label: "MP3", title: "320kbps，檔案小，適合 Podcast 或試聽" },
];
// Optional loudness normalisation (EX-01); the server measures the whole mix first, then applies one gain.
const LOUDNESS = [
  { value: "", label: "不調整", title: "維持混音原本的音量" },
  { value: "-14", label: "-14 LUFS", title: "YouTube、Spotify 等串流平台的播放音量" },
  { value: "-16", label: "-16 LUFS", title: "Podcast 常用的音量" },
];
export const loudnessValue = (v) => (v ? Number(v) : null);

export function LoudnessField({ value, onChange }) {
  return html`<${Field} label="響度標準化" hint="選填。先量整段混音的響度，再整體調整到目標值，峰值不超過 -1 dBTP；會多花一次讀取聲音的時間">
    <${Segmented} value=${value} onChange=${onChange} options=${LOUDNESS} />
  <//>`;
}

// The resolution choices with this project's sizes; one equal to the project size is left out.
export const resolutionOptions = (settings) => RESOLUTIONS.map((r) => {
  const size = r.value ? resolutionSize(r.value, settings) : [settings.width, settings.height];
  return size && { ...r, label: `${r.label} ${size[0]} × ${size[1]}` };
}).filter(Boolean);

function queued(job) {
  toast(`已加入佇列：${job.title}`, {
    action: { label: "查看進度", fn: () => navigate("library", { libraryTab: "exports" }) },
  });
}

// The server answers with an attachment, so the app window stays on this page.
function downloadSrt(pid) {
  const a = document.createElement("a");
  a.href = `/api/projects/${encodeURIComponent(pid)}/captions.srt`;
  a.click();
}

function Summary({ project, clips, size }) {
  const s = project.settings;
  return html`<div class="exp-summary">
    <div><span>鏡頭</span><b>${clips.length}</b></div>
    <div><span>長度</span><b>${fmtDuration(sequenceDuration(clips))}</b></div>
    <div><span>尺寸</span><b title=${size ? `專案尺寸 ${s.width} × ${s.height}，${aspectDiffers(size, s)
      ? "輸出時縮放" : "直接用輸出尺寸合成"}` : undefined}>
      ${size ? `${size[0]} × ${size[1]}` : `${s.width} × ${s.height}`}</b></div>
    <div><span>影格率</span><b>${s.fps} fps</b></div>
    <div><span>字幕</span><b>${(project.captions || []).length}</b></div>
    <div><span>音軌</span><b>${(project.audio || []).length}</b></div>
  </div>`;
}

// Each form is a hook returning { body, footer } so both keep their state while the user flips tabs.
function useVideoForm(project, clips, close) {
  const { settings, system, jobs } = getState();
  const defaults = (settings && settings.export) || {};
  const ffmpegOk = !!(system && system.ffmpeg && system.ffmpeg.available);
  const [preset, setPreset] = useState(defaults.preset || "");
  const [resolution, setResolution] = useState(defaults.resolution || "");
  const [quality, setQuality] = useState(defaults.quality || "high");
  const [encoder, setEncoder] = useState("auto");
  const [burn, setBurn] = useState(!!defaults.burnCaptions);
  const [srt, setSrt] = useState(false);
  const [fadeIn, setFadeIn] = useState(project.settings.fadeIn || 0);
  const [fadeOut, setFadeOut] = useState(project.settings.fadeOut || 0);
  const [clipAudio, setClipAudio] = useState(true);
  const [loudness, setLoudness] = useState("");
  const [fit, setFit] = useState(project.settings.fit || "contain");
  const [keep, setKeep] = useState(false);
  const [busy, setBusy] = useState(false);
  // Same rule as the SRT endpoint, which answers 409 when every caption is blank.
  const hasCaptions = (project.captions || []).some((c) => (c.text || "").trim());
  const noClips = "序列裡沒有任何鏡頭，請確認鏡頭有勾選「放入序列」";
  const blocked = !clips.length ? noClips : !ffmpegOk ? "找不到 ffmpeg" : "";
  const out = preset ? presetOutput(preset, project.settings) : { size: resolutionSize(resolution, project.settings), maxrate: 0 };
  const [outW, outH] = out.size || [project.settings.width, project.settings.height];
  const estimate = clips.length ? estimateExport(jobs, sequenceDuration(clips), outW * outH, encoder, quality) : null;

  const submit = async () => {
    setBusy(true);
    const s = project.settings;
    if ((s.fadeIn || 0) !== fadeIn || (s.fadeOut || 0) !== fadeOut || (s.fit || "contain") !== fit) {
      await updateProject({ settings: { fadeIn, fadeOut, fit } }, { label: "修改輸出設定" });
    }
    if (keep) await saveSettings({ export: { quality, burnCaptions: burn, preset, resolution } });
    const job = await submitJob("export", null, {
      options: { quality, encoder, burnCaptions: burn, fadeIn, fadeOut, clipAudio, fit,
        size: out.size, maxrate: out.maxrate, loudness: loudnessValue(loudness) },
    }, { quiet: true });
    setBusy(false);
    if (!job) return;
    if (!burn && srt && hasCaptions) downloadSrt(project.id);
    queued(job);
    close(job);
  };
  const srtOnly = () => {
    downloadSrt(project.id);
    toast("已下載 SRT 字幕檔");
  };

  const footer = html`
    <${ExportFolderNote} class="modal-foot-note" />
    <${Button} icon="captions" disabled=${!hasCaptions}
      title=${hasCaptions ? "不輸出影片，直接下載目前的字幕" : "這個專案還沒有字幕"} onClick=${srtOnly}>只下載 SRT<//>
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="film" busy=${busy} disabled=${!!blocked} title=${blocked || undefined}
      onClick=${submit}>開始輸出<//>`;

  const markers = (project.markers || []).length;
  const body = html`
    <${Summary} project=${project} clips=${clips} size=${out.size} />
    ${clips.length > 0 && html`<p class="dim exp-estimate">${estimate
      ? `預估輸出時間約 ${fmtDuration(Math.max(1, estimate.seconds))}（依最近 ${estimate.count} 次輸出的速度）`
      : "第一次輸出後才能估算輸出時間"}</p>`}
    ${clips.length > 0 && html`<${ExportCheck} pid=${project.id} />`}
    ${!ffmpegOk && html`<div class="note note-warn">
      <${Icon} name="alert" size=${15} />
      <span class="note-text">找不到 ffmpeg，無法輸出影片。請到設定頁的「依賴元件」按「一鍵安裝」，或指定 ffmpeg.exe 位置後重新偵測。</span>
      <${Button} size="sm" onClick=${() => { close(null); navigate("settings"); }}>前往設定<//>
    </div>`}
    ${!clips.length && html`<div class="note note-warn">
      <${Icon} name="alert" size=${15} /><span class="note-text">${noClips}</span>
    </div>`}
    <${Field} label="平台預設" hint=${preset
      ? `輸出 ${outW} × ${outH}，位元率上限 ${out.maxrate / 1000} Mbps${aspectDiffers(out.size, project.settings)
        ? "。和專案比例不同，由「畫面比例不同時」決定補黑邊或裁切" : ""}`
      : "依上架平台調整輸出尺寸與位元率，不改專案本身"}>
      <div class="exp-preset">
        <${Select} value=${preset} onChange=${setPreset} options=${PRESETS} />
        ${markers > 0 && html`<${Button} size="sm" icon="copy" title="依時間軸標記產生章節列表，貼到 YouTube 影片說明"
          onClick=${() => copyChapters(project)}>複製 YouTube 章節<//>`}
      </div>
    <//>
    ${!preset && html`<${Field} label="輸出尺寸" hint="和專案同比例，整段直接用這個尺寸合成，放大過的畫面不會先縮回專案尺寸">
      <${Segmented} value=${out.size ? resolution : ""} onChange=${setResolution} options=${resolutionOptions(project.settings)} />
    <//>`}
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
      hint=${hasCaptions ? "字型、大小與位置照預覽畫面；關掉時影片不含字幕，可以另外下載 SRT 字幕檔" : "這個專案還沒有字幕，打開也不會有作用"} />
    ${!burn && hasCaptions && html`<${Toggle} checked=${srt} onChange=${setSrt} label="同時下載 SRT 字幕檔"
      hint="按「開始輸出」時一併下載到「下載」資料夾" />`}
    <${Toggle} checked=${clipAudio} onChange=${setClipAudio} label="保留影片片段本身的聲音"
      hint="關掉時只會有音軌上的音樂與旁白" />
    <${LoudnessField} value=${loudness} onChange=${setLoudness} />
    <${Toggle} checked=${keep} onChange=${setKeep} label="把平台預設、輸出尺寸、畫質與字幕設定存成之後的預設" />`;
  return { body, footer };
}

// The sequence's mix alone: the same fades and clip-sound choice as the video, without the picture.
function useAudioForm(project, clips, close) {
  const { system } = getState();
  const ffmpegOk = !!(system && system.ffmpeg && system.ffmpeg.available);
  const [format, setFormat] = useState("wav");
  const [fadeIn, setFadeIn] = useState(project.settings.fadeIn || 0);
  const [fadeOut, setFadeOut] = useState(project.settings.fadeOut || 0);
  const [clipAudio, setClipAudio] = useState(true);
  const [loudness, setLoudness] = useState("");
  const [busy, setBusy] = useState(false);
  const blocked = !clips.length ? "序列裡沒有任何鏡頭，請確認鏡頭有勾選「放入序列」" : !ffmpegOk ? "找不到 ffmpeg" : "";
  const submit = async () => {
    setBusy(true);
    const job = await submitJob("export", null, { options: { format, fadeIn, fadeOut, clipAudio, loudness: loudnessValue(loudness) } }, { quiet: true });
    setBusy(false);
    if (!job) return;
    queued(job);
    close(job);
  };
  const footer = html`
    <${ExportFolderNote} class="modal-foot-note" />
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="music" busy=${busy} disabled=${!!blocked} title=${blocked || undefined}
      onClick=${submit}>開始輸出<//>`;
  const body = html`
    <p class="dim exp-lede">只輸出整條序列混好的聲音（影片片段、子母畫面與所有音軌），長度與影片相同，不含畫面。</p>
    ${blocked && html`<div class="note note-warn">
      <${Icon} name="alert" size=${15} /><span class="note-text">${blocked}</span>
    </div>`}
    <${Field} label="格式">
      <${Segmented} value=${format} onChange=${setFormat} options=${AUDIO_FORMATS} />
    <//>
    <div class="row-2">
      <${Field} label="開頭淡入">
        <${NumberInput} value=${fadeIn} onCommit=${setFadeIn} min=${0} max=${10} step=${0.1} digits=${1} suffix="秒" />
      <//>
      <${Field} label="結尾淡出">
        <${NumberInput} value=${fadeOut} onCommit=${setFadeOut} min=${0} max=${10} step=${0.1} digits=${1} suffix="秒" />
      <//>
    </div>
    <${Toggle} checked=${clipAudio} onChange=${setClipAudio} label="保留影片片段本身的聲音"
      hint="關掉時只會有音軌上的音樂與旁白" />
    <${LoudnessField} value=${loudness} onChange=${setLoudness} />`;
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
    <${ExportFolderNote} class="modal-foot-note" label="ZIP 檔存到" />
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
  const audio = useAudioForm(project, clips, close);
  const pack = usePackageForm(project, close, fullBackup);
  const form = mode === "package" ? pack : mode === "audio" ? audio : video;
  return html`<${Modal} title="輸出" width=${600} backdropClose=${false} onClose=${() => close(null)} footer=${form.footer} class="export-dialog">
    <${Tabs} class="exp-modes" value=${mode} tabs=${MODES} onChange=${setMode} />
    ${form.body}
  <//>`;
}

// mode: "video", "audio" or "package"; fullBackup starts the package as a restorable backup (匯出專案).
// Resolves with the submitted job, or null when canceled.
export function openExportDialog(mode = "video", { fullBackup = false } = {}) {
  const project = getState().project;
  if (!project) {
    toast("請先開啟專案", { level: "warn" });
    return Promise.resolve(null);
  }
  const initial = ["package", "audio"].includes(mode) ? mode : "video";
  return openModal((close) => html`<${ExportBody} project=${project} initial=${initial} fullBackup=${fullBackup} close=${close} />`);
}
