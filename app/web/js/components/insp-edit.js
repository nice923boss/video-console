// Shot inspector, "剪輯" tab: where the shot sits in the output sequence, which part of its video
// plays (in/out points) and the transition into the next shot. Numbers here follow sequence.js,
// so they match what the timeline draws and what the export renders.
import { html, useMemo } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { updateShot } from "../lib/actions.js";
import { submitJob } from "../lib/jobs-actions.js";
import { clock, setTime } from "../lib/clock.js";
import { fmtTime, fmtDuration, round } from "../lib/format.js";
import { buildSequence, clipBounds, videoTake, imageTake } from "../lib/sequence.js";
import { Icon } from "./icons.js";
import { Field, NumberInput, Select, Toggle, Section, Button, Badge, DraftText } from "./ui.js";
import { ColorSwatches } from "./insp-content.js";
import { toast } from "./toast.js";

const NONE = [];
const MIN_LEN = 0.1;
const DEFAULT_TRANSITION = { type: "cut", duration: 0.5 };

function Hint({ icon = "info", tone = "", children }) {
  return html`<p class=${`insp-hint ${tone}`}><${Icon} name=${icon} size=${14} /><span>${children}</span></p>`;
}

// Where the playhead falls inside this clip, in source seconds; null when it is elsewhere.
function playheadSource(clip) {
  if (!clip) return null;
  const t = clock.time;
  if (t < clip.start - 1e-3 || t > clip.start + clip.length + 1e-3) return null;
  return clip.in + Math.min(clip.length, Math.max(0, t - clip.start));
}

function SequenceSection({ shot, clip, position, total }) {
  const inSeq = shot.inSequence !== false;
  return html`<${Section} title="輸出序列" icon="rows">
    <${Toggle} checked=${inSeq} label="放入輸出序列" hint="關掉後預覽與匯出都會跳過這個鏡頭，生成結果仍會保留"
      onChange=${(v) => updateShot(shot.id, { inSequence: v }, { label: v ? "放入序列" : "移出序列" })} />
    ${clip && html`<div class="seq-pos">
      <div class="seq-pos-text">
        <span class="dim">第 ${position} 段，共 ${total} 段</span>
        <span class="mono">${fmtTime(clip.start)} 到 ${fmtTime(clip.start + clip.length)}</span>
      </div>
      <${Button} size="sm" kind="ghost" icon="skip-back" title="把播放頭移到這個鏡頭的開頭"
        onClick=${() => setTime(clip.start + 1e-3)}>跳到這裡<//>
    </div>`}
  <//>`;
}

function TrimBar({ dur, tIn, tOut }) {
  const pct = (v) => `${round((v / dur) * 100, 2)}%`;
  return html`<div class="trim-bar" style=${`--in:${pct(tIn)};--out:${pct(tOut)}`} aria-hidden="true">
    <i class="trim-range"></i>
  </div>`;
}

function TrimSection({ project, shot, vid, clip }) {
  const dur = Number(vid.duration);
  const [tIn, tOut] = clipBounds(project, shot);
  const trimmed = (shot.trimIn || 0) > 0 || shot.trimOut != null;
  const setIn = (v) => {
    const next = round(Math.min(Math.max(0, v ?? 0), tOut - MIN_LEN), 3);
    updateShot(shot.id, { trimIn: next }, { label: "修改入點" });
  };
  const setOut = (v) => {
    const next = v == null ? null : round(Math.max(tIn + MIN_LEN, Math.min(dur, v)), 3);
    updateShot(shot.id, { trimOut: next == null || next >= dur - 1e-3 ? null : next }, { label: "修改出點" });
  };
  const fromPlayhead = (set) => () => {
    if (!clip) {
      toast("這個鏡頭不在輸出序列裡，播放頭無法對應到它", { level: "warn" });
      return;
    }
    const src = playheadSource(clip);
    if (src == null) {
      toast("播放頭不在這個鏡頭的範圍內，請先在時間軸上移到這個鏡頭", { level: "warn" });
      return;
    }
    set(src);
  };

  return html`<${Section} title="剪輯點" icon="scissors"
      actions=${trimmed && html`<${Button} size="sm" kind="ghost" icon="undo"
        onClick=${() => updateShot(shot.id, { trimIn: 0, trimOut: null }, { label: "重設剪輯點" })}>重設<//>`}>
    <${TrimBar} dur=${dur} tIn=${tIn} tOut=${tOut} />
    <div class="trim-legend mono"><span>0</span><span>使用 ${fmtDuration(tOut - tIn)}，原長 ${fmtDuration(dur)}</span><span>${fmtTime(dur)}</span></div>
    <div class="row-2">
      <${Field} label="入點" after=${html`<${Button} size="sm" kind="ghost" icon="frame" title="用目前播放頭的位置當入點"
          onClick=${fromPlayhead(setIn)}>播放頭<//>`}>
        <${NumberInput} value=${tIn} min=${0} max=${round(tOut - MIN_LEN, 3)} step=${0.1} digits=${2} suffix="秒"
          onCommit=${setIn} />
      <//>
      <${Field} label="出點" after=${html`<${Button} size="sm" kind="ghost" icon="frame" title="用目前播放頭的位置當出點"
          onClick=${fromPlayhead(setOut)}>播放頭<//>`}>
        <${NumberInput} value=${shot.trimOut ?? null} nullable placeholder=${`結尾 ${round(dur, 2)}`}
          min=${round(tIn + MIN_LEN, 3)} max=${round(dur, 3)} step=${0.1} digits=${2} suffix="秒" onCommit=${setOut} />
      <//>
    </div>
    <${Hint}>也可以在時間軸上拖曳片段的左右邊緣來修剪；出點留白代表播到影片結尾。<//>
  <//>`;
}

function StillSection({ shot, img }) {
  return html`<${Section} title="長度" icon="clock">
    <${Field} label=${img ? "圖片在序列中的長度" : "預留長度"}>
      <${NumberInput} value=${shot.duration} min=${0.2} max=${600} step=${0.5} digits=${2} suffix="秒"
        onCommit=${(duration) => updateShot(shot.id, { duration }, { label: "修改片長" })} />
    <//>
    <${Hint}>
      ${img
        ? "這個鏡頭目前只有圖片，序列中會以靜態畫面顯示；生成影片後會改用影片長度與剪輯點。"
        : "這個鏡頭還沒有圖片或影片，預覽與匯出時會顯示鏡頭名稱的字卡。"}
    <//>
  <//>`;
}

function TransitionSection({ shot, clip, isLast, transitions }) {
  const t = { ...DEFAULT_TRANSITION, ...(shot.transition || {}) };
  const cut = t.type === "cut";
  const set = (patch, label) => updateShot(shot.id, { transition: { ...t, ...patch } }, { label });
  const applied = clip && clip.transition;
  const clamped = applied && !cut && applied.type !== "cut" && Math.abs(applied.duration - t.duration) > 0.01;
  let note = null;
  if (!cut && shot.inSequence === false) note = "這個鏡頭不在輸出序列裡，轉場不會套用。";
  else if (!cut && isLast) note = "這是序列中的最後一個鏡頭，後面沒有鏡頭可以接，轉場不會套用。";
  else if (clamped) note = `前後鏡頭的長度不夠，實際轉場會縮短為 ${round(applied.duration, 2)} 秒。`;

  return html`<${Section} title="接到下一鏡" icon="split">
    <div class="row-2">
      <${Field} label="轉場">
        <${Select} value=${t.type} options=${transitions.map((x) => ({ value: x.id, label: x.label }))}
          onChange=${(type) => set({ type }, "修改轉場")} />
      <//>
      <${Field} label="轉場長度">
        <${NumberInput} value=${t.duration} min=${0.1} max=${5} step=${0.1} digits=${2} suffix="秒" disabled=${cut}
          onCommit=${(duration) => set({ duration }, "修改轉場長度")} />
      <//>
    </div>
    ${note && html`<${Hint} icon="alert" tone="warn-text">${note}<//>`}
    ${!note && !cut && html`<${Hint}>轉場會和下一個鏡頭重疊，序列總長度會因此縮短。<//>`}
  <//>`;
}

function SourceInfo({ vid, img }) {
  const take = vid || img;
  if (!take) return null;
  return html`<div class="src-info">
    <${Badge} tone=${vid ? "blue" : "neutral"}>${vid ? "影片" : "圖片"}<//>
    ${take.width > 0 && html`<span class="mono">${take.width} × ${take.height}</span>`}
    ${vid && vid.fps > 0 && html`<span class="mono">${round(vid.fps, 2)} fps</span>`}
    ${vid && html`<span>${vid.hasAudio ? "有聲音" : "無聲音"}</span>`}
  </div>`;
}

// Colour tag and note, to mark a clip for review (a long pause, a retake) without cutting it yet.
function MarkSection({ shot }) {
  return html`<${Section} title="標記" icon="palette">
    <${Field} label="標籤顏色" hint="時間軸上的片段會整段染色；在時間軸片段上按右鍵也能設定">
      <${ColorSwatches} value=${shot.color ?? null}
        onChange=${(color) => updateShot(shot.id, { color }, { label: "修改標籤顏色" })} />
    <//>
    <${Field} label="備註">
      <${DraftText} multiline rows=${2} value=${shot.notes || ""} maxLength=${4000} placeholder="例如：停頓 3 秒，評估是否刪除"
        onCommit=${(notes) => updateShot(shot.id, { notes }, { coalesce: `${shot.id}:notes`, label: "修改鏡頭備註" })} />
    <//>
  <//>`;
}

// Speech-to-subtitle turns the clip's own sound into captions at the clip's place on the timeline.
function TranscribeSection({ project, shot, vid, clip, busy }) {
  if (!vid || !vid.hasAudio) return null;
  const job = busy.find((j) => j.kind === "transcribe");
  const end = clip ? clip.start + clip.length : 0;
  const overlap = clip ? project.captions.filter((c) => c.end > clip.start && c.start < end).length : 0;
  const label = !job ? "轉成字幕" : job.status === "running" ? `轉錄中 ${Math.round((job.progress || 0) * 100)}%` : "排隊中";
  return html`<${Section} title="語音轉字幕" icon="captions">
    <${Button} icon="captions" busy=${!!job} disabled=${!clip || !!job} onClick=${() => submitJob("transcribe", shot.id)}>${label}<//>
    ${!clip && html`<${Hint} icon="alert" tone="warn-text">這個鏡頭不在序列裡，放回序列後才能轉成字幕。<//>`}
    ${clip && overlap > 0 && html`<${Hint} icon="alert" tone="warn-text">片段範圍內已有 ${overlap} 句字幕，轉出的字幕會另外加入，不會取代。<//>`}
    ${clip && !overlap && html`<${Hint}>辨識修剪範圍內的原聲，字幕直接加到時間軸；不滿意可以按復原整批收回。<//>`}
  <//>`;
}

export function EditTab({ project, shot, busy = NONE }) {
  const transitions = useStore((s) => s.presets.transitions || NONE);
  const clips = useMemo(() => buildSequence(project), [project]);
  const pos = clips.findIndex((c) => c.shot.id === shot.id);
  const clip = pos >= 0 ? clips[pos] : null;
  const vid = videoTake(project, shot);
  const img = vid ? null : imageTake(project, shot);

  return html`
    <${SourceInfo} vid=${vid} img=${img} />
    <${SequenceSection} shot=${shot} clip=${clip} position=${pos + 1} total=${clips.length} />
    <${MarkSection} shot=${shot} />
    ${vid
      ? html`<${TrimSection} project=${project} shot=${shot} vid=${vid} clip=${clip} />`
      : html`<${StillSection} shot=${shot} img=${img} />`}
    <${TransitionSection} shot=${shot} clip=${clip} isLast=${pos >= 0 && pos === clips.length - 1} transitions=${transitions} />
    <${TranscribeSection} project=${project} shot=${shot} vid=${vid} clip=${clip} busy=${busy} />`;
}
