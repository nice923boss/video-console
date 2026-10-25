// Batch export (EX-06): queue the video export of several projects at once. Quality, output size, captions and
// loudness are picked once for all; each project keeps its own fades and framing. The export lane runs one
// job at a time, so the projects render one after another in the queue.
import { html, useState } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { submitBatch } from "../lib/jobs-actions.js";
import { RESOLUTIONS, resolutionSize } from "../lib/export-presets.js";
import { fmtDuration } from "../lib/format.js";
import { QUALITY, LoudnessField, loudnessValue } from "./export-dialog.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, Segmented, Toggle } from "../components/ui.js";
import { ExportFolderNote } from "../components/export-folder.js";

function BatchExport({ close }) {
  const { projects, settings, system } = getState();
  const defaults = (settings && settings.export) || {};
  const ffmpegOk = !!(system && system.ffmpeg && system.ffmpeg.available);
  const list = [...projects].sort((a, b) => (b.updated || 0) - (a.updated || 0));
  const ready = list.filter((p) => p.clips > 0);
  const [picked, setPicked] = useState(() => new Set());
  const [quality, setQuality] = useState(defaults.quality || "high");
  const [resolution, setResolution] = useState(defaults.resolution || "");
  const [burn, setBurn] = useState(!!defaults.burnCaptions);
  const [loudness, setLoudness] = useState("");
  const [busy, setBusy] = useState(false);

  const toggle = (id, on) => setPicked((prev) => {
    const next = new Set(prev);
    if (on) next.add(id);
    else next.delete(id);
    return next;
  });
  const all = picked.size === ready.length;
  const sizeOf = (p) => {
    const s = p.settings || {};
    const size = resolutionSize(resolution, s) || [s.width, s.height];
    return `${size[0]} × ${size[1]}`;
  };

  const submit = async () => {
    setBusy(true);
    const jobs = ready.filter((p) => picked.has(p.id)).map((p) => ({
      kind: "export", pid: p.id, shotId: null,
      payload: { options: { quality, burnCaptions: burn, captionSize: defaults.captionSize || 52,
        size: resolutionSize(resolution, p.settings || {}), loudness: loudnessValue(loudness) } },
    }));
    const res = await submitBatch(jobs);
    setBusy(false);
    if (res && res.jobs.length) close(res);
  };

  const blocked = !ffmpegOk ? "找不到 ffmpeg" : !picked.size ? "先勾選要輸出的專案" : "";
  const footer = html`
    <${ExportFolderNote} class="modal-foot-note" />
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="film" busy=${busy} disabled=${!!blocked} title=${blocked || undefined}
      onClick=${submit}>${picked.size ? `輸出 ${picked.size} 個專案` : "開始輸出"}<//>`;

  return html`<${Modal} title="批次輸出" width=${600} backdropClose=${false} onClose=${() => close(null)} footer=${footer}>
    <div class="bx-head">
      <span class="dim">勾選要輸出的專案，排進佇列後一個接一個輸出成 MP4。</span>
      <span class="grow"></span>
      <${Button} size="sm" kind="ghost" disabled=${!ready.length}
        onClick=${() => setPicked(all ? new Set() : new Set(ready.map((p) => p.id)))}>${all && ready.length ? "全部取消" : "全選"}<//>
    </div>
    <div class="bx-list">
      ${list.map((p) => {
        const empty = !(p.clips > 0);
        return html`<label key=${p.id} class=${`bx-row ${empty ? "is-off" : ""}`}
            title=${empty ? "序列裡沒有任何鏡頭，無法輸出" : undefined}>
          <input type="checkbox" checked=${picked.has(p.id)} disabled=${empty}
            onChange=${(e) => toggle(p.id, e.currentTarget.checked)} />
          <span class="grow bx-name">${p.name}</span>
          <span class="dim">${empty ? "沒有鏡頭" : `${p.clips} 個鏡頭`}</span>
          <span class="dim mono">${p.duration > 0 ? fmtDuration(p.duration) : ""}</span>
          <span class="dim mono">${p.settings ? sizeOf(p) : ""}</span>
        </label>`;
      })}
    </div>
    <div class="row-2">
      <${Field} label="畫質">
        <${Segmented} value=${quality} onChange=${setQuality} options=${QUALITY} />
      <//>
      <${Field} label="輸出尺寸" hint="和各專案同比例；短邊 1080 或 2160 像素">
        <${Segmented} value=${resolution} onChange=${setResolution} options=${RESOLUTIONS} />
      <//>
    </div>
    <${Toggle} checked=${burn} onChange=${setBurn} label="把字幕燒進畫面" hint="沒有字幕的專案不受影響" />
    <${LoudnessField} value=${loudness} onChange=${setLoudness} />
    <p class="dim">淡入淡出與畫面比例用各專案自己的設定；要個別調整，請開啟專案後用「輸出影片」。</p>
  <//>`;
}

export const openBatchExport = () => openModal((close) => html`<${BatchExport} close=${close} />`);
