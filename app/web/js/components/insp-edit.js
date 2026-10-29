// Shot inspector, "剪輯" tab: where the shot sits in the output sequence, which part of its video
// plays (in/out points) and the transition into the next shot. Numbers here follow sequence.js,
// so they match what the timeline draws and what the export renders.
import { html, useMemo } from "../lib/h.js";
import { setState, useStore } from "../lib/store.js";
import { ops, updateShot } from "../lib/actions.js";
import { submitJob } from "../lib/jobs-actions.js";
import { clock, setTime } from "../lib/clock.js";
import { fmtTime, fmtDuration, round } from "../lib/format.js";
import { buildSequence, clipBounds, videoTake, imageTake } from "../lib/sequence.js";
import { Icon } from "./icons.js";
import { Field, NumberInput, Select, Toggle, Section, Button, Badge, DraftText, Slider } from "./ui.js";
import { ColorSwatches } from "./insp-content.js";
import { toast } from "./toast.js";
import { keyHint } from "../lib/keymap.js";
import { reverseClip, detachAudio, isReverse, stabilizeClip, isStabilized, addKeyframe } from "./tl-commands.js";
import { KeyList, useClipTime } from "./key-panel.js";
import { valueAt, withKey } from "../lib/keyframes.js";
import { GradeEditor } from "./grade-panel.js";
import { MaskEditor } from "./mask-panel.js";
import { ReviewSection } from "./review-list.js";
import { fullGrade, isNeutral } from "../lib/grade.js";
import { findLoud } from "./tl-wave.js";

const NONE = [];
const MIN_LEN = 0.1;
const DEFAULT_TRANSITION = { type: "cut", duration: 0.5 };
const SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4];

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
  const held = Object.values(shot.parked || {}).reduce((n, list) => n + list.length, 0);
  return html`<${Section} title="輸出序列" icon="rows">
    <${Toggle} checked=${inSeq} label="放入輸出序列" hint="關掉後預覽與匯出都會跳過這個鏡頭，生成結果仍會保留"
      onChange=${(v) => updateShot(shot.id, { inSequence: v }, { label: v ? "放入序列" : "移出序列" })} />
    ${held > 0 && html`<${Hint}>${held} 個字幕、音訊與畫面隨鏡頭收起，放回序列時回到鏡頭上的原位。<//>`}
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

// 壓低爆音 on the shot's own sound: the version's volume keys (file seconds, so trims keep them) are worked out
// afresh at the shot volume, so pressing again after moving the slider fits the dips to it.
async function declipShot(project, shot, clip) {
  const vid = clip.take;
  const speed = clip.speed || 1;
  const gainAt = (t) => Number(shot.volume ?? 1) * valueAt({ keyframes: vid.gainKeys || [] }, "volume", clip.in + t * speed, 1);
  const found = await findLoud(project.id, vid, clip.in, clip.length, speed, gainAt);
  if (!found) return toast("這段影片的波形沒有音量資料，無法偵測爆音", { level: "warn" });
  const had = (vid.volumeKeys || []).length > 0;
  if (!found.spans.length && !had) return toast("這段原聲沒有爆音");
  const volumeKeys = found.keys.map((k) => ({ t: round(clip.in + k.t * speed, 3), volume: k.volume }));
  if (!(await ops([{ op: "take.update", id: vid.id, patch: { volumeKeys } }], { label: "壓低爆音" }))) return;
  toast(found.spans.length ? `已壓低 ${found.spans.length} 段爆音` : "目前的音量不會爆音，已移除先前的壓低");
}

// Speed, reverse, the shot's own sound volume and detached sound. The same commands sit on edit.speed, edit.reverseClip and
// edit.detachAudio (tl-commands.js); edit.speed focuses the select here.
function SpeedSection({ project, shot, vid, clips, busy }) {
  const speed = Number(shot.speed || 1);
  const detached = !!shot.audioDetached;
  const reversed = isReverse(vid);
  const rendering = busy.some((j) => j.kind === "reverse");
  const sel = { type: "shot", id: shot.id };
  const clip = clips.find((c) => c.shot.id === shot.id);
  const options = (SPEEDS.includes(speed) ? SPEEDS : [...SPEEDS, speed].sort((a, b) => a - b))
    .map((v) => ({ value: v, label: `${v}x` }));
  let note = null;
  if (detached) note = `原聲已分離到音訊軌，要先還原原聲${keyHint("edit.detachAudio")}才能變速；倒放只換畫面，原聲照常順向播放。`;

  return html`<${Section} title="速度與方向" icon="clock">
    <div class="row-2">
      <${Field} label=${`速度${keyHint("edit.speed")}`}>
        <${Select} class="insp-speed" value=${speed} options=${options} disabled=${detached}
          onChange=${(v) => updateShot(shot.id, { speed: v }, { label: "變速" })} />
      <//>
      <${Field} label="方向">
        <${Button} icon="undo" busy=${rendering} disabled=${rendering}
          title=${`倒放${keyHint("edit.reverseClip")}；第一次會先產生倒放的影片`}
          onClick=${() => reverseClip(project, clips, sel)}>${rendering ? "產生中" : reversed ? "取消倒放" : "倒放"}<//>
      <//>
    </div>
    ${vid.hasAudio && !detached && html`<${Field} label="原聲音量" hint="100% 是原始音量，最高 400%；預覽播放最高只到 100%">
      <${Slider} value=${Number(shot.volume ?? 1)} min=${0} max=${4} step=${0.05} format=${(v) => `${Math.round(v * 100)}%`}
        onCommit=${(v) => updateShot(shot.id, { volume: round(v, 2) }, { label: "原聲音量" })} />
    <//>
    <div class="row">
      <${Button} size="sm" icon="volume" disabled=${!clip}
        title=${clip ? "找出這段原聲超過滿格（時間軸波形變紅）的地方，用音量關鍵幀自動壓低到不爆音；調過原聲音量後再按一次會重新計算"
          : "鏡頭不在輸出序列裡，無法偵測爆音"}
        onClick=${() => declipShot(project, shot, clip)}>壓低爆音<//>
      ${(vid.volumeKeys || []).length > 0 && html`<${Button} size="sm" icon="x" title="移除自動壓低的音量關鍵幀，恢復原本的音量"
        onClick=${() => ops([{ op: "take.update", id: vid.id, patch: { volumeKeys: [] } }], { label: "移除壓低爆音" })}>移除壓低<//>`}
    </div>
    ${clip && html`<${SourceKeys} project=${project} shot=${shot} clip=${clip} clips=${clips} />`}`}
    ${vid.hasAudio && html`<${Button} icon="music"
      title=${`${detached ? "刪除分離出的音訊，鏡頭改回播放自己的聲音" : "把原聲移到音訊軌，可以單獨修剪、淡入淡出、加音量關鍵幀"}${keyHint("edit.detachAudio")}`}
      onClick=${() => detachAudio(project, clips, sel)}>${detached ? "還原原聲" : "分離原聲"}<//>`}
    ${vid.hasAudio && html`<${Button} icon="wand" title="降噪、去回音與選聲道都在分離出的音訊上調整；還沒分離就先分離原聲"
      onClick=${() => voiceTools(project, shot, clips, sel)}>降噪／去回音<//>`}
    ${note && html`<${Hint}>${note}<//>`}
  <//>`;
}

// The shot's hand-set sound keys (the version's gainKeys: file seconds, factors on the shot volume), shown in
// seconds into the clip and at the level they play: the playhead's level and the key list, so a key placed
// with K is set right under 原聲音量. Removing the last key or clearing them moves their level into the volume.
function SourceKeys({ project, shot, clip, clips }) {
  const speed = clip.speed || 1;
  const gain = clip.take.gainKeys || NONE;
  const item = { keyframes: gain.map((k) => ({ ...k, t: (k.t - clip.in) / speed })) };
  const local = useClipTime(clip.start, clip.length, gain.length > 0);
  const base = Number(shot.volume ?? 1);
  const update = (patch, label) => {
    const list = [];
    if (patch.keyframes) {
      const gainKeys = patch.keyframes.map((k) => ({ ...k, t: round(clip.in + k.t * speed, 3) }));
      list.push({ op: "take.update", id: clip.take.id, patch: { gainKeys } });
    }
    if (patch.volume != null) list.push({ op: "shot.update", id: shot.id, patch: { volume: round(Math.min(4, base * patch.volume), 2) } });
    return ops(list, { label });
  };
  return html`${gain.length > 0 && base > 0 && html`<${Field} label="播放頭位置的音量"
      hint="原聲有關鍵幀：這裡改寫播放頭位置的關鍵幀；上面的原聲音量整段一起放大縮小">
      <${Slider} value=${base * valueAt(item, "volume", local, 1)} min=${0} max=${Math.min(4, base * 4)} step=${0.05}
        format=${(v) => `${Math.round(v * 100)}%`}
        onCommit=${(v) => update({ keyframes: withKey(item, local, { volume: round(v / base, 4) }) }, "原聲關鍵幀音量")} />
    <//>`}
    <${KeyList} item=${item} start=${clip.start} length=${clip.length} local=${local} update=${update}
      onAdd=${() => addKeyframe(project, clips, { type: "shot", id: shot.id })} />`;
}

// Ticked = the shot plays a stabilized copy of its range (edits.py stabilize); the first tick renders it.
function StabilizeSection({ shot, vid, busy }) {
  const rendering = busy.some((j) => j.kind === "stabilize");
  return html`<${Section} title="防抖" icon="camera">
    <${Toggle} checked=${isStabilized(vid) || rendering} disabled=${rendering} onChange=${() => stabilizeClip(shot.id)}
      label=${rendering ? "防抖處理中" : "防抖"}
      hint="只處理目前的剪輯範圍，前後各多留 1 秒；畫面會稍微放大來裁掉晃動的黑邊。之後剪輯範圍拉長超過多留的部分，要取消再重新勾選" />
  <//>`;
}

// 降噪 / 去回音 / 聲道 belong to an audio clip (voice-panel.js): opens the shot's detached sound, detaching it first.
function voiceTools(project, shot, clips, sel) {
  const audio = shot.audioDetached && (project.audio || []).find((a) => a.shotId === shot.id);
  if (audio) setState({ editSel: { type: "audio", id: audio.id } });
  else detachAudio(project, clips, sel);
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

// The shot's own colour grade (GR-01): only its picture, under any adjustment layer above it.
function GradeSection({ shot }) {
  return html`<${Section} title="鏡頭調色" icon="palette" collapsible defaultOpen=${false} id="shot-grade"
      actions=${!isNeutral(shot.grade) && html`<${Badge} tone="blue">已調色<//>`}>
    <${GradeEditor} id=${shot.id} saved=${fullGrade(shot.grade)}
      onSave=${(grade, label) => updateShot(shot.id, { grade }, { label: `鏡頭${label}` })} />
    <${Hint}>只改這個鏡頭的畫面；時間軸上的調整圖層會再疊在它上面。<//>
  <//>`;
}

function MaskSection({ shot }) {
  const count = (shot.masks || []).length;
  return html`<${Section} title="遮罩（模糊／馬賽克）" icon="eye-off" collapsible defaultOpen=${false} id="shot-masks"
      actions=${count > 0 && html`<${Badge} tone="blue">${count} 個<//>`}>
    <${MaskEditor} shot=${shot} />
    <${Hint}>遮住人臉或商標；框固定在畫面上，不會跟著移動的物體走，也不蓋到子母畫面與字幕。<//>
  <//>`;
}

// Speech-to-subtitle turns the clip's own sound into captions at the clip's place on the timeline.
function TranscribeSection({ project, shot, vid, clip, busy }) {
  if (!vid || !vid.hasAudio) return null;
  const job = busy.find((j) => j.kind === "transcribe");
  const end = clip ? clip.start + clip.length : 0;
  // captions the last run on this shot added are replaced by the next run; any others in range stay
  const previous = project.captions.filter((c) => c.sttShot === shot.id).length;
  const overlap = clip ? project.captions.filter((c) => c.sttShot !== shot.id && c.end > clip.start && c.start < end).length : 0;
  const label = !job ? (previous ? "重新轉成字幕" : "轉成字幕") : job.status === "running" ? `轉錄中 ${Math.round((job.progress || 0) * 100)}%` : "排隊中";
  return html`<${Section} title="語音轉字幕" icon="captions">
    <${Button} icon="captions" busy=${!!job} disabled=${!clip || !!job} onClick=${() => submitJob("transcribe", shot.id)}>${label}<//>
    ${!clip && html`<${Hint} icon="alert" tone="warn-text">這個鏡頭不在序列裡，放回序列後才能轉成字幕。<//>`}
    ${clip && previous > 0 && html`<${Hint} icon="alert" tone="warn-text">上次從這個片段轉出的 ${previous} 句字幕會被新結果取代（包含手動改過的），按復原可整批還原。<//>`}
    ${clip && overlap > 0 && html`<${Hint} icon="alert" tone="warn-text">片段範圍內另有 ${overlap} 句其他字幕，會保留，新字幕另外加入。<//>`}
    ${clip && !previous && !overlap && html`<${Hint}>辨識修剪範圍內的原聲，字幕直接加到時間軸；不滿意可以按復原整批收回。<//>`}
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
    <${ReviewSection} id="insp-review" items=${shot.review}
      onChange=${(review) => updateShot(shot.id, { review }, { label: "刪除客戶意見" })} />
    ${vid
      ? html`<${TrimSection} project=${project} shot=${shot} vid=${vid} clip=${clip} />
        <${SpeedSection} project=${project} shot=${shot} vid=${vid} clips=${clips} busy=${busy} />
        <${StabilizeSection} shot=${shot} vid=${vid} busy=${busy} />`
      : html`<${StillSection} shot=${shot} img=${img} />`}
    ${(vid || img) && html`<${GradeSection} shot=${shot} />`}
    ${(vid || img) && html`<${MaskSection} shot=${shot} />`}
    <${TransitionSection} shot=${shot} clip=${clip} isLast=${pos >= 0 && pos === clips.length - 1} transitions=${transitions} />
    <${TranscribeSection} project=${project} shot=${shot} vid=${vid} clip=${clip} busy=${busy} />`;
}
