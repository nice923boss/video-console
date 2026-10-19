// One job in the queue list: status, where it runs, progress, errors and the actions that fit its
// state. The details dialog shows what was actually sent (seed, size, template or provider), which
// is the first thing to check when a result looks wrong.
import { html } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { revealJob } from "../lib/actions.js";
import { cancelJob, retryJob, moveJobTop, removeJob } from "../lib/jobs-actions.js";
import { fmtDuration, fmtRelTime, fmtDateTime, KIND_LABEL, STATUS_LABEL } from "../lib/format.js";
import { Icon } from "./icons.js";
import { Badge, IconButton, Progress, Spinner } from "./ui.js";
import { Thumb } from "./media.js";
import { openMenu, openModal, Modal } from "./overlay.js";
import { toast } from "./toast.js";

const NONE = [];
const FINISHED = new Set(["done", "failed", "canceled", "interrupted"]);
const STATUS_ICON = { queued: "clock", done: "check-circle", failed: "x-circle", canceled: "x", interrupted: "alert" };
const STATUS_TONE = { queued: "neutral", running: "accent", done: "ok", failed: "danger", canceled: "neutral", interrupted: "warn" };
const BACKEND_LABEL = { comfy: "ComfyUI", provider: "雲端 API", local: "本機" };

const nowSec = () => Date.now() / 1000;

// "ComfyUI · Z-Image Turbo", "雲端 API · fal.ai 圖片", "本機 ffmpeg".
export function laneLabel(job, { providers = NONE, workflows = NONE, comfy = NONE } = {}) {
  const p = job.payload || {};
  // Audio8Voice jobs share the "comfy" lane only to keep the GPU to one job at a time; VoAI ones run in the cloud
  if (job.kind === "voice") return job.lane === "provider:voai" ? "VoAI 雲端" : "Audio8Voice";
  if (job.kind === "upscalevideo") return "本機 Video2X";
  if (p.backend === "provider" || String(job.lane || "").startsWith("provider:")) {
    const prov = providers.find((x) => x.id === p.provider);
    return `雲端 API · ${prov ? prov.name : p.provider || "未知供應商"}`;
  }
  if (p.backend === "comfy" || job.lane === "comfy") {
    const wf = workflows.find((x) => x.id === p.workflow);
    const server = p.server && p.server !== "auto" ? comfy.find((c) => c.id === p.server) : null;
    return ["ComfyUI", server && server.name, wf && wf.name].filter(Boolean).join(" · ");
  }
  if (job.lane === "local:export") return "本機輸出";
  return "本機 ffmpeg";
}

function timing(job) {
  if (job.status === "queued") return `排隊 ${fmtDuration(Math.max(0, nowSec() - (job.created || nowSec())))}`;
  if (job.status === "running") return `已執行 ${fmtDuration(Math.max(0, nowSec() - (job.started || nowSec())))}`;
  const parts = [];
  if (job.seconds != null) parts.push(`用時 ${fmtDuration(job.seconds)}`);
  if (job.finished) parts.push(fmtRelTime(job.finished));
  return parts.join("，");
}

function copyText(text, what) {
  navigator.clipboard.writeText(text).then(
    () => toast(`已複製${what}`, { duration: 1800 }),
    () => toast("無法存取剪貼簿", { level: "warn" }),
  );
}

function detailRows(job, lane) {
  const p = job.payload || {};
  const size = p.width && p.height ? `${p.width} × ${p.height}` : "";
  return [
    ["類型", KIND_LABEL[job.kind] || job.kind],
    ["狀態", STATUS_LABEL[job.status] || job.status],
    ["執行位置", lane],
    ["建立時間", fmtDateTime(job.created)],
    ["開始時間", job.started ? fmtDateTime(job.started) : ""],
    ["結束時間", job.finished ? fmtDateTime(job.finished) : ""],
    ["用時", job.seconds != null ? fmtDuration(job.seconds) : ""],
    ["嘗試次數", job.attempt > 1 ? `第 ${job.attempt} 次` : ""],
    ["種子", p.seed != null ? `${p.seed}${p.seedLocked ? "（已鎖定）" : ""}` : ""],
    ["尺寸", size],
    ["長度", p.duration ? fmtDuration(p.duration) : ""],
    ["參考圖強度", p.denoise != null ? `重繪強度 ${p.denoise}` : ""],
  ].filter(([, v]) => v);
}

function JobDetails({ job, lane, close }) {
  const p = job.payload || {};
  return html`<${Modal} title=${job.title} width=${560} onClose=${() => close()}>
    <dl class="kv">
      ${detailRows(job, lane).map(([k, v]) => html`<div key=${k} class="kv-row"><dt>${k}</dt><dd>${v}</dd></div>`)}
    </dl>
    ${p.prompt && html`<div class="job-detail-block">
      <div class="job-detail-head"><span>送出的描述</span>
        <${IconButton} icon="copy" size=${14} title="複製描述" onClick=${() => copyText(p.prompt, "描述")} /></div>
      <pre class="job-detail-text">${p.prompt}</pre>
    </div>`}
    ${p.negative && html`<div class="job-detail-block">
      <div class="job-detail-head"><span>負向描述</span></div>
      <pre class="job-detail-text">${p.negative}</pre>
    </div>`}
    ${job.error && html`<div class="job-detail-block">
      <div class="job-detail-head"><span class="danger-text">錯誤訊息</span>
        <${IconButton} icon="copy" size=${14} title="複製錯誤訊息" onClick=${() => copyText(job.error, "錯誤訊息")} /></div>
      <pre class="job-detail-text">${job.error}</pre>
    </div>`}
    ${(job.notes || NONE).length > 0 && html`<div class="job-detail-block">
      <div class="job-detail-head"><span>備註</span></div>
      <ul class="job-notes">${job.notes.map((n, i) => html`<li key=${i}>${n}</li>`)}</ul>
    </div>`}
  <//>`;
}

function JobLead({ job, project }) {
  const takeId = job.result && (job.result.takeIds || NONE)[0];
  const take = project && project.id === job.pid && takeId ? project.takes[takeId] : null;
  if (job.status === "done" && take) {
    return html`<${Thumb} pid=${project.id} path=${take.thumb || take.file} ratio="1/1" class="job-thumb" />`;
  }
  if (job.status === "running" && job.previewRev) {
    return html`<div class="thumb thumb-cover job-thumb">
      <img src=${`/api/jobs/${encodeURIComponent(job.id)}/preview?v=${job.previewRev}`} alt="" draggable="false" />
    </div>`;
  }
  return html`<span class=${`job-icon tone-${STATUS_TONE[job.status] || "neutral"}`}>
    ${job.status === "running" ? html`<${Spinner} size=${18} />` : html`<${Icon} name=${STATUS_ICON[job.status] || "dot"} size=${18} />`}
  </span>`;
}

export function JobRow({ job, project = null, projectName = "" }) {
  const ctx = useStore((s) => ({ providers: s.providers, workflows: s.workflows, comfy: s.comfy }));
  const lane = laneLabel(job, ctx);
  const finished = FINISHED.has(job.status);
  const running = job.status === "running";
  const openDetails = () => openModal((close) => html`<${JobDetails} job=${job} lane=${lane} close=${close} />`);
  const menu = (e) => openMenu(e, [
    { label: "查看詳細資料", icon: "info", onClick: openDetails },
    { label: "前往結果", icon: "arrow-right", onClick: () => revealJob(job.id) },
    job.status === "queued" && { label: "移到最前面", icon: "to-top", onClick: () => moveJobTop(job.id) },
    job.error && { label: "複製錯誤訊息", icon: "copy", onClick: () => copyText(job.error, "錯誤訊息") },
    finished && job.status !== "done" && { label: "重試", icon: "retry", onClick: () => retryJob(job.id) },
    { divider: true },
    !finished && { label: "取消工作", icon: "x", danger: true, onClick: () => cancelJob(job.id) },
    finished && { label: "從清單移除", icon: "trash", onClick: () => removeJob(job.id) },
  ]);

  return html`<div class=${`job-row is-${job.status}`} onDblClick=${() => revealJob(job.id)}>
    <div class="job-lead"><${JobLead} job=${job} project=${project} /></div>
    <div class="job-main">
      <div class="job-line">
        <span class="job-title" title=${job.title}>${job.title}</span>
        <${Badge} tone=${STATUS_TONE[job.status] || "neutral"}>${STATUS_LABEL[job.status] || job.status}<//>
        ${job.attempt > 1 && html`<${Badge} tone="neutral" title="這是重試後的工作">第 ${job.attempt} 次<//>`}
      </div>
      <div class="job-sub dim">
        ${projectName && html`<span class="job-proj">${projectName}</span>`}
        <span>${lane}</span>
        <span class="mono">${timing(job)}</span>
      </div>
      ${running && html`<div class="job-progress">
        <${Progress} value=${job.progress || 0} />
        <span class="mono">${Math.round((job.progress || 0) * 100)}%</span>
      </div>`}
      ${!finished && job.message && html`<div class="job-msg" title=${job.message}>${job.message}</div>`}
      ${job.status === "failed" && job.error && html`<div class="job-error danger-text" title=${job.error}>${job.error}</div>`}
      ${job.status === "interrupted" && html`<div class="job-error warn-text">程式關閉時這個工作還沒完成，可以按「重試」重新送出</div>`}
      ${(job.notes || NONE).length > 0 && html`<div class="job-note warn-text" title=${job.notes.join("\n")}>
        <${Icon} name="info" size=${13} />${job.notes[0]}${job.notes.length > 1 ? `（另有 ${job.notes.length - 1} 則）` : ""}
      </div>`}
    </div>
    <div class="job-actions">
      ${job.status === "queued" && html`<${IconButton} icon="to-top" size=${16} title="移到最前面" onClick=${() => moveJobTop(job.id)} />`}
      ${finished && job.status !== "done" && html`<${IconButton} icon="retry" size=${16} title="重試" onClick=${() => retryJob(job.id)} />`}
      ${job.status === "done" && html`<${IconButton} icon="arrow-right" size=${16} title="前往結果" onClick=${() => revealJob(job.id)} />`}
      ${!finished && html`<${IconButton} icon="x" size=${16} title="取消工作" onClick=${() => cancelJob(job.id)} />`}
      <${IconButton} icon="more" size=${16} title="更多動作" onClick=${menu} />
    </div>
  </div>`;
}
