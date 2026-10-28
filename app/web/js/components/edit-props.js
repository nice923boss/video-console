// Right-hand panel of the edit view. A selected soundtrack item, caption, picture-in-picture clip or
// adjustment layer gets its own property panel; anything else falls back to the shot inspector.
import { html, useMemo } from "../lib/h.js";
import { setState, useStore } from "../lib/store.js";
import { ops } from "../lib/actions.js";
import { clock, setTime } from "../lib/clock.js";
import { clamp, fmtSeconds, round, shotLabel } from "../lib/format.js";
import { audioLength, audioMedia, audioSpeed, audioTrack, VOICE_TRACK, overlayHeight, fitFrame, fillFrame, buildSequence, clipIndexAt } from "../lib/sequence.js";
import { rowRole, roleOp, duckDb, ROLE_LABELS, DUCK_LEVELS } from "../lib/duck.js";
import { hasKeys, valueAt } from "../lib/keyframes.js";
import { withDips } from "../lib/declip.js";
import { VOICE_STATES, voiceState, voiceClip, voiceEnd, generateVoices } from "../lib/voice.js";
import { openCastDialog } from "../dialogs/cast-dialog.js";
import { openCaptionStyleDialog } from "../dialogs/caption-style-dialog.js";
import { DEFAULT_RATE, readChars, maxChars, tooLong, openCaptionCondense } from "../dialogs/caption-condense.js";
import {
  legacySize, styleById, defaultStyleId, resolveStyle, dragScope, setDragScope, setDefaultOps, useForAllOps,
} from "../lib/caption-style.js";
import { Button, IconButton, Field, DraftText, NumberInput, Slider, Segmented, Toggle, Select, Badge } from "./ui.js";
import { Icon } from "./icons.js";
import { Inspector } from "./inspector.js";
import { VoicePanel } from "./voice-panel.js";
import { GradePanel } from "./grade-panel.js";
import { KeyList, keyedPatch, useClipTime } from "./key-panel.js";
import { addKeyframe, isStabilized, stabilizeClip } from "./tl-commands.js";
import { clipKeysHint, keyHint, undoKeyText } from "../lib/keymap.js";
import { multiAudio } from "./tl-tracks.js";
import { findLoud, playGain } from "./tl-wave.js";
import { toast } from "./toast.js";
import { CHOICES as GAIN_CHOICES, dbToGain } from "../dialogs/import-gain.js";
import { DEFAULT_CHROMA } from "../lib/chroma.js";
import { parseGlossary, glossaryHits, fixHits, hitText } from "../lib/glossary.js";

const MAX_TIME = 36000;
const MIN_LEN = 0.1;
const POSITIONS = [
  { value: "top", label: "上方" },
  { value: "middle", label: "中間" },
  { value: "bottom", label: "下方" },
];

const DRAG_SCOPES = [
  { value: "one", label: "只移動這句" },
  { value: "style", label: "同樣式全部", title: "拖曳一句字幕，用同一個樣式的字幕都移到那裡" },
];

const updateAudio = (id, patch, label) => ops([{ op: "audio.update", id, patch }], { label });
const updateCaption = (id, patch, label, coalesce = null) => ops([{ op: "caption.update", id, patch }], { label, coalesce });
const closePanel = () => setState({ editSel: null });

function Head({ icon, title, sub }) {
  return html`<header class="insp-head">
    <div class="insp-icon"><${Icon} name=${icon} size=${22} /></div>
    <div class="insp-title">
      <h2 title=${title}>${title}</h2>
      <span class="insp-sub">${sub}</span>
    </div>
    <div class="insp-nav">
      <${IconButton} icon="x" size=${16} title=${`回到鏡頭屬性${keyHint("edit.deselect")}`} onClick=${closePanel} />
    </div>
  </header>`;
}

function Seconds({ value, onCommit, min = 0, max = MAX_TIME, nullable = false, placeholder = "" }) {
  return html`<${NumberInput} value=${value} onCommit=${onCommit} min=${min} max=${max} step=${0.1} digits=${3}
    suffix="秒" nullable=${nullable} placeholder=${placeholder} />`;
}

function AudioPanel({ project, item }) {
  const media = audioMedia(project, item);
  const full = media && media.duration ? Number(media.duration) : 0;
  const start = Number(item.start || 0);
  const tIn = Number(item.in || 0);
  const len = audioLength(item, media);
  const outLimit = full > 0 ? full : MAX_TIME;
  const name = item.name || (media && media.name) || "音訊";
  const pct = (v) => `${Math.round(v * 100)}%`;
  const local = useClipTime(start, len, (item.keyframes || []).length > 0);
  const here = () => clamp(clock.time - start, 0, len);
  const remove = () => {
    closePanel();
    ops([{ op: "audio.delete", id: item.id }], { label: "移除音軌" });
  };
  // 壓低爆音: dips multiplied into the volume keyframes the clip already has, so hand-made keys keep their shape.
  const declip = async () => {
    const room = 200 - (item.keyframes || []).filter((k) => "volume" in k).length;
    const found = await findLoud(project.id, media, tIn, len, audioSpeed(item), (t) => playGain(item, t, len), room);
    if (!found) return toast("這段音訊的波形沒有音量資料，無法偵測爆音", { level: "warn" });
    if (!found.spans.length) return toast("這段音訊沒有爆音");
    if (!(await updateAudio(item.id, { keyframes: withDips(item, found.keys, len) }, "壓低爆音"))) return;
    toast(`已壓低 ${found.spans.length} 段爆音`);
  };
  // Ducking (AU-02): the role is the whole row's, the 人聲 switch this clip's alone.
  const tracks = project.tracks || {};
  const row = audioTrack(item);
  const role = rowRole(tracks, row);
  const db = duckDb(project.settings);

  return html`<div class="insp-shot">
    <${Head} icon=${item.muted ? "volume-x" : "music"} title=${name}
      sub=${html`開始 ${fmtSeconds(start, 2)}<i></i>長 ${fmtSeconds(len, 2)}${item.speed && item.speed !== 1 ? html`<i></i>${item.speed}x` : ""}${item.muted ? html`<i></i><span class="warn-text">靜音</span>` : ""}`} />
    <div class="insp-scroll">
      ${!media && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
        <span class="note-text">找不到這條音軌的音訊檔，可能已從素材庫刪除。匯出時會略過它。</span></div>`}
      ${item.captionId && html`<div class="note note-info"><${Icon} name="mic" size=${15} />
        <span class="note-text">這是一句字幕的 AI 配音。修改字幕文字或人物後，到字幕屬性重新生成會換掉這條音軌。</span>
        <${Button} size="sm" icon="captions" disabled=${!(project.captions || []).some((c) => c.id === item.captionId)}
          onClick=${() => setState({ editSel: { type: "caption", id: item.captionId } })}>選取字幕<//>
      </div>`}
      <${Field} label="名稱">
        <${DraftText} value=${item.name || ""} placeholder=${name} maxlength="120"
          onCommit=${(v) => updateAudio(item.id, { name: v.trim() }, "音軌名稱")} />
      <//>
      <${Field} label="在序列中的開始時間" hint="也可以直接在時間軸上拖曳音軌">
        <${Seconds} value=${start} onCommit=${(v) => updateAudio(item.id, { start: v }, "移動音軌")} />
      <//>
      <div class="row-2">
        <${Field} label="入點（檔案內）">
          <${Seconds} value=${tIn} max=${Math.max(0, (item.out ?? outLimit) - MIN_LEN)}
            onCommit=${(v) => updateAudio(item.id, { in: v }, "修改音軌入點")} />
        <//>
        <${Field} label="出點（檔案內）">
          <${Seconds} value=${item.out ?? null} min=${tIn + MIN_LEN} max=${outLimit} nullable placeholder="檔案結尾"
            onCommit=${(v) => updateAudio(item.id, { out: v != null && full > 0 && v >= full - 1e-3 ? null : v }, "修改音軌出點")} />
        <//>
      </div>
      <${Field} label="音量" hint=${hasKeys(item, "volume") ? "音量有關鍵幀：這裡顯示並改寫播放頭位置的音量"
        : "100% 是原始音量，最高 400%；預覽播放最高只到 100%"}>
        <${Slider} value=${Number(valueAt(item, "volume", local, 1))} min=${0} max=${4} step=${0.05} format=${pct}
          onCommit=${(v) => updateAudio(item.id, keyedPatch(item, here(), { volume: round(v, 2) }), "音軌音量")} />
      <//>
      ${media && html`<div class="row"><${Button} size="sm" icon="volume" onClick=${declip}
        title="找出這段音訊超過滿格（時間軸波形變紅）的地方，加音量關鍵幀自動壓低到不爆音；已有的關鍵幀會保留">壓低爆音<//></div>`}
      <div class="row-2">
        <${Field} label="淡入">
          <${Seconds} value=${Number(item.fadeIn || 0)} max=${30} onCommit=${(v) => updateAudio(item.id, { fadeIn: v }, "音軌淡入")} />
        <//>
        <${Field} label="淡出">
          <${Seconds} value=${Number(item.fadeOut || 0)} max=${30} onCommit=${(v) => updateAudio(item.id, { fadeOut: v }, "音軌淡出")} />
        <//>
      </div>
      <${Toggle} checked=${!!item.muted} label="靜音" hint="保留在時間軸上，但預覽與匯出都不播放"
        onChange=${(muted) => updateAudio(item.id, { muted }, muted ? "音軌靜音" : "取消靜音")} />
      <${Field} label="所在音軌的用途" hint=${role === "bgm" ? "這條音軌遇到人聲自動降低，說完再拉回"
        : role === "speech" ? "背景音樂軌在這條音軌出聲時自動降低" : "一般音軌不參與自動避讓，適合放音效"}>
        <${Segmented} size="sm" value=${role} onChange=${(r) => ops([roleOp(tracks, row, r)], { label: `音軌用途：${ROLE_LABELS[r]}` })}
          options=${(row === VOICE_TRACK ? ["", "speech"] : ["", "speech", "bgm"]).map((r) => ({ value: r, label: ROLE_LABELS[r] }))} />
      <//>
      ${role === "bgm" && html`<${Field} label="遇到人聲降低" hint="整個專案的背景音樂軌共用這個幅度">
        <${Segmented} size="sm" value=${db} options=${DUCK_LEVELS.map((d) => ({ value: d, label: `${d} dB` }))}
          onChange=${(d) => ops([{ op: "project.update", patch: { settings: { duckDb: d } } }], { label: `背景音樂降低 ${d} dB` })} />
      <//>`}
      ${!role && html`<${Toggle} checked=${!!item.speech} label="這段是人聲"
        hint="只把這一段算成人聲：背景音樂軌遇到它會自動降低"
        onChange=${(speech) => updateAudio(item.id, { speech }, speech ? "標成人聲" : "取消標成人聲")} />`}
      ${!item.captionId && html`<${Toggle} checked=${!!item.fixed} label="固定在時間軸"
        hint="開啟連動時，移動、刪除或修剪鏡頭也不會帶動這條音軌，適合跟著節拍剪的配樂"
        onChange=${(fixed) => updateAudio(item.id, { fixed }, fixed ? "固定音軌" : "取消固定音軌")} />`}
      ${media && html`<${VoicePanel} project=${project} item=${item} media=${media} />`}
      <${KeyList} item=${item} start=${start} length=${len} local=${local}
        update=${(patch, label) => updateAudio(item.id, patch, label)}
        onAdd=${() => addKeyframe(project, null, { type: "audio", id: item.id })} />
      ${media && html`<div class="src-info dim">
        <${Icon} name="music" size=${14} /><span class="grow">${media.name || media.file}</span>
        <span class="mono">${full > 0 ? fmtSeconds(full, 2) : "長度未知"}</span>
      </div>`}
      <div class="row">
        <${Button} size="sm" icon="skip-back" onClick=${() => setTime(start)}>跳到開頭<//>
        <${Button} size="sm" icon="arrow-right" onClick=${() => updateAudio(item.id, { start: round(clock.time, 3) }, "移動音軌")}
          title="把這條音軌的開頭對齊目前播放頭">移到播放頭<//>
      </div>
    </div>
    <div class="insp-foot">
      <span class="insp-hint dim">${clipKeysHint("移除")}</span>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${remove}>移除音軌<//>
    </div>
  </div>`;
}

function OverlayPanel({ project, item }) {
  const media = (project.media || {})[item.mediaId];
  const video = !!media && media.kind === "video";
  const full = video && media.duration ? Number(media.duration) : 0;
  const start = Number(item.start || 0);
  const tIn = Number(item.in || 0);
  const len = Number(item.length || 0);
  const layers = project.layers || [];
  const li = layers.findIndex((l) => l.id === item.layer);
  const track = li >= 0 ? layers[li].name || `畫面 ${li + 2}` : "畫面軌";
  const name = item.name || (media && media.name) || "片段";
  const pct = (v) => `${Math.round(v * 100)}%`;
  const update = (patch, label) => ops([{ op: "overlay.update", id: item.id, patch }], { label });
  const keyed = (item.keyframes || []).length > 0;
  const local = useClipTime(start, len, keyed);
  const at = (f, fallback) => Number(valueAt(item, f, local, fallback));
  // Fields with keyframes change their key at the playhead.
  const set = (values, label) => update(keyedPatch(item, clamp(clock.time - start, 0, len), values), label);
  const fw = Number(project.settings.width);
  const fh = Number(project.settings.height);
  const w = at("w", 0.35);
  const h = overlayHeight({ ...item, w }, media, fw, fh);
  const mx = 0.03;
  const my = (0.03 * fw) / fh;
  const place = ({ x, y, w: size }) =>
    set({ x: round(clamp(x, -4, 4), 4), y: round(clamp(y, -4, 4), 4), ...(size ? { w: size } : {}) }, "片段位置");
  const spots = [
    { label: "滿版", ...fitFrame(media, fw, fh) },
    { label: "填滿", ...fillFrame(media, fw, fh) },
    { label: "左上", x: mx, y: my },
    { label: "右上", x: 1 - w - mx, y: my },
    { label: "置中", x: (1 - w) / 2, y: (1 - h) / 2 },
    { label: "左下", x: mx, y: 1 - h - my },
    { label: "右下", x: 1 - w - mx, y: 1 - h - my },
  ];
  const remove = () => {
    closePanel();
    ops([{ op: "overlay.delete", id: item.id }], { label: "移除片段" });
  };

  return html`<div class="insp-shot">
    <${Head} icon=${video ? "video" : "image"} title=${name}
      sub=${html`${track}<i></i>開始 ${fmtSeconds(start, 2)}<i></i>長 ${fmtSeconds(len, 2)}`} />
    <div class="insp-scroll">
      ${!media && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
        <span class="note-text">找不到這個片段的素材檔，可能已從素材庫刪除。匯出時會略過它。</span></div>`}
      <${Field} label="名稱">
        <${DraftText} value=${item.name || ""} placeholder=${name} maxlength="120"
          onCommit=${(v) => update({ name: v.trim() }, "片段名稱")} />
      <//>
      <div class="row-2">
        <${Field} label="開始" hint="也可以在時間軸上拖曳，上下拖曳換軌道">
          <${Seconds} value=${start} onCommit=${(v) => update({ start: v }, "移動片段")} />
        <//>
        <${Field} label="長度">
          <${Seconds} value=${len} min=${MIN_LEN} max=${video ? (full > 0 ? full - tIn : MAX_TIME) : 600}
            onCommit=${(v) => update({ length: v }, "修剪片段")} />
        <//>
      </div>
      ${video && html`<${Field} label="入點（檔案內）">
        <${Seconds} value=${tIn} max=${full > 0 ? Math.max(0, full - MIN_LEN) : MAX_TIME}
          onCommit=${(v) => update(full > 0 ? { in: v, length: round(Math.min(len, full - v), 3) } : { in: v }, "修改片段入點")} />
      <//>`}
      <${Field} label="位置" hint=${keyed ? "這個片段有關鍵幀：下面顯示播放頭位置的值，有關鍵幀的項目會改寫播放頭位置的關鍵幀"
        : "以畫面左上角為起點；滿版顯示整張素材，填滿蓋滿畫面並裁掉超出的部分"}>
        <div class="row">
          ${spots.map((s) => html`<${Button} size="sm" kind="ghost" onClick=${() => place(s)}>${s.label}<//>`)}
        </div>
      <//>
      <div class="row-2">
        <${Field} label="水平">
          <${Slider} value=${at("x", 0)} min=${Math.min(-1, at("x", 0))} max=${Math.max(1, at("x", 0))} step=${0.005} format=${pct}
            onCommit=${(v) => set({ x: round(v, 4) }, "片段位置")} />
        <//>
        <${Field} label="垂直">
          <${Slider} value=${at("y", 0)} min=${Math.min(-1, at("y", 0))} max=${Math.max(1, at("y", 0))} step=${0.005} format=${pct}
            onCommit=${(v) => set({ y: round(v, 4) }, "片段位置")} />
        <//>
      </div>
      <${Field} label="大小" hint="畫面寬度的百分比，高度依素材比例；超過 100% 時超出畫面的部分會被裁掉">
        <${Slider} value=${w} min=${0.05} max=${Math.max(2, w)} step=${0.01} format=${pct}
          onCommit=${(v) => set({ w: round(v, 4) }, "片段大小")} />
      <//>
      <${Field} label="不透明度">
        <${Slider} value=${at("opacity", 1)} min=${0} max=${1} step=${0.01} format=${pct}
          onCommit=${(v) => set({ opacity: round(v, 2) }, "片段不透明度")} />
      <//>
      <${ChromaField} chroma=${item.chroma} update=${update} />
      ${video && html`<${StabilizeField} item=${item} media=${media} />`}
      ${video && media.hasAudio && html`<${Field} label="音量" hint="100% 是原始音量，最高 400%；預覽播放最高只到 100%">
        <${Slider} value=${at("volume", 1)} min=${0} max=${4} step=${0.05} format=${pct}
          onCommit=${(v) => set({ volume: round(v, 2) }, "片段音量")} />
      <//>`}
      <${KeyList} item=${item} start=${start} length=${len} local=${local} update=${update}
        onAdd=${() => addKeyframe(project, null, { type: "overlay", id: item.id })} />
      ${media && html`<div class="src-info dim">
        <${Icon} name=${video ? "video" : "image"} size=${14} /><span class="grow">${media.name || media.file}</span>
        <span class="mono">${full > 0 ? fmtSeconds(full, 2) : video ? "長度未知" : "圖片"}</span>
      </div>`}
      <div class="row">
        <${Button} size="sm" icon="skip-back" onClick=${() => setTime(start)}>跳到開頭<//>
        <${Button} size="sm" icon="arrow-right" onClick=${() => update({ start: round(clock.time, 3) }, "移動片段")}
          title="把這個片段的開頭對齊目前播放頭">移到播放頭<//>
      </div>
    </div>
    <div class="insp-foot">
      <span class="insp-hint dim">${clipKeysHint("移除")}</span>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${remove}>移除片段<//>
    </div>
  </div>`;
}

// Ticked when the clip shows a stabilized copy of its video (a library item made by a "stabilize" job).
function StabilizeField({ item, media }) {
  const { pid, jobs } = useStore((s) => ({ pid: s.pid, jobs: s.jobs }));
  const rendering = jobs.some((j) => j.pid === pid && j.kind === "stabilize" && j.payload && j.payload.overlay === item.id
    && (j.status === "queued" || j.status === "running"));
  return html`<${Field} label="防抖" hint="只處理片段用到的範圍，前後各多留 1 秒，處理好的影片會放進素材庫；畫面會稍微放大來裁掉晃動的黑邊。之後片段拉長超過多留的部分，要取消再重新勾選">
    <${Toggle} checked=${isStabilized(media) || rendering} disabled=${rendering}
      label=${rendering ? "防抖處理中" : "防抖"} onChange=${() => stabilizeClip(item.id, "overlay")} />
  <//>`;
}

// Colour key (GR-09): pixels near the chosen colour turn transparent, for a clip shot on a green or blue screen.
function ChromaField({ chroma, update }) {
  const c = { ...DEFAULT_CHROMA, ...chroma };
  const save = (patch, label) => update({ chroma: { ...c, ...patch } }, label);
  const pct = (v) => `${Math.round(v * 100)}%`;
  return html`<${Field} label="去背（綠幕）" hint="把接近指定顏色的部分變透明；預覽是近似效果，邊緣以匯出為準">
      <${Toggle} checked=${c.on} label=${c.on ? "已開啟" : "關閉"} onChange=${(on) => save({ on }, on ? "開啟去背" : "關閉去背")} />
    <//>
    ${c.on && html`<${Field} label="去背顏色" hint="點色塊挑選要去掉的顏色，綠幕用綠色，藍幕用藍色">
      <div class="row">
        <label class="route-color" style=${`--route:${c.color}`} title="去背顏色">
          <input type="color" value=${c.color} onChange=${(e) => save({ color: e.currentTarget.value }, "去背顏色")} />
        </label>
        <span class="mono dim">${c.color}</span>
        <span class="grow"></span>
        <${Button} size="sm" kind="ghost" onClick=${() => save({ color: "#00ff00" }, "去背顏色")}>綠幕<//>
        <${Button} size="sm" kind="ghost" onClick=${() => save({ color: "#0000ff" }, "去背顏色")}>藍幕<//>
      </div>
    <//>
    <div class="row-2">
      <${Field} label="容許範圍" hint="越大去掉越多相近的顏色">
        <${Slider} value=${c.similarity} min=${0.01} max=${0.6} step=${0.01} format=${pct}
          onCommit=${(v) => save({ similarity: round(v, 2) }, "去背容許範圍")} />
      <//>
      <${Field} label="邊緣柔化" hint="0 是硬邊，越大邊緣越柔">
        <${Slider} value=${c.blend} min=${0} max=${0.5} step=${0.01} format=${pct}
          onCommit=${(v) => save({ blend: round(v, 2) }, "去背邊緣柔化")} />
      <//>
    </div>`}`;
}

function VoiceField({ project, cap }) {
  const jobs = useStore((s) => s.jobs);
  const cast = project.cast || [];
  const state = voiceState(project, cap, jobs);
  const info = VOICE_STATES[state];
  const clip = voiceClip(project, cap.id);
  const clipLen = clip ? audioLength(clip, (project.media || {})[clip.mediaId]) : 0;
  const over = clipLen - (cap.end - cap.start);
  const fitEnd = clip ? voiceEnd(project, cap, clipLen) : cap.end;
  const options = [{ value: null, label: "未指定" }, ...cast.map((m) => ({ value: m.id, label: m.name || "未命名" }))];
  const canRun = ["none", "stale", "ready"].includes(state);
  return html`<${Field} label="人物與配音" hint="配音放在這句字幕的開始時間；字幕或人物改了之後可以重新生成">
    <div class="combo-row">
      <${Select} value=${cap.speaker || null} options=${options} title="誰說這句話"
        onChange=${(speaker) => updateCaption(cap.id, { speaker }, "字幕人物")} />
      <${Button} size="sm" kind="ghost" icon="users" onClick=${openCastDialog} title="新增人物、挑選聲音">人物表<//>
    </div>
    <div class="voice-row">
      <${Badge} tone=${info.tone}>${info.label}<//>
      ${clip && html`<button type="button" class="voice-clip" title="選取這句的配音音軌"
        onClick=${() => setState({ editSel: { type: "audio", id: clip.id } })}>
        配音 ${fmtSeconds(clipLen, 1)}${over > 0.3 ? html`<span class="warn-text">，比字幕長 ${fmtSeconds(over, 1)}</span>` : ""}</button>`}
      <span class="grow"></span>
      <${Button} size="sm" icon="mic" disabled=${!canRun} onClick=${() => generateVoices([cap.id])}>
        ${state === "ready" || state === "stale" ? "重新生成" : "生成配音"}<//>
    </div>
    ${over > 0.05 && html`<div class="voice-row">
      <${Button} size="sm" kind="ghost" icon="arrow-right" disabled=${fitEnd <= cap.end + 1e-3}
        title=${fitEnd <= cap.end + 1e-3 ? "下一句字幕緊接在後，字幕無法再延長"
          : `字幕結尾延長到 ${fmtSeconds(fitEnd, 2)}${fitEnd < cap.start + clipLen - 1e-3 ? "（到下一句字幕開頭為止）" : ""}`}
        onClick=${() => updateCaption(cap.id, { end: fitEnd }, "字幕對齊配音長度")}>對齊配音長度<//>
    </div>`}
  <//>`;
}

function ShotField({ project, cap }) {
  const shots = project.shots || [];
  const clips = buildSequence(project);
  const under = clips[clipIndexAt(clips, cap.start)];
  const options = [
    { value: null, label: "不綁定" },
    ...shots.map((s, i) => ({ value: s.id, label: `${shotLabel(s, i)}${under && under.shot.id === s.id ? "（字幕開頭所在）" : ""}` })),
  ];
  const bind = (shot) => ops([{ op: "caption.update", id: cap.id, patch: { shot } }],
    { label: shot ? "綁定鏡頭" : "解除綁定鏡頭" });
  return html`<${Field} label="對應鏡頭" hint="綁定後分鏡卡會顯示這句字幕，兩邊改文字會同步；時間與長度不受影響，一個鏡頭可以綁定多句">
    <${Select} value=${cap.shot || null} options=${options} title="這句字幕顯示在哪個分鏡" onChange=${bind} />
  <//>`;
}

function StyleField({ project, cap }) {
  const legacy = useStore((s) => legacySize(s.settings));
  const def = styleById(project, defaultStyleId(project));
  const own = styleById(project, cap.style) ? cap.style : null;
  const style = resolveStyle(project, cap, legacy);
  const options = [
    { value: null, label: `專案預設（${def ? def.name : "內建樣式"}）` },
    ...(project.captionStyles || []).map((s) => ({ value: s.id, label: s.name || "未命名" })),
  ];
  const edit = () => openCaptionStyleDialog({ styleId: style.id, text: cap.text || "", row: cap.position || "bottom" });
  return html`<${Field} label="字幕樣式" hint="改樣式會更新所有用這個樣式的字幕">
    <div class="combo-row">
      <${Select} value=${own} options=${options} title="這句字幕用的樣式"
        onChange=${(id) => updateCaption(cap.id, { style: id }, "字幕樣式")} />
      <${Button} size="sm" kind="ghost" icon="palette" onClick=${edit} title="外框、陰影、字型、顏色與樣式庫">編輯樣式<//>
    </div>
    <div class="row">
      <${Button} size="sm" icon="layers" title="設為專案預設，並讓每句字幕都改用這個樣式"
        onClick=${() => ops(useForAllOps(project, style.id), { label: "全部字幕套用同一樣式" })}>全部字幕套用此樣式<//>
      ${own && own !== (def && def.id) && html`<${Button} size="sm" icon="check" title="沒有指定樣式的字幕都用這個"
        onClick=${() => ops(setDefaultOps(own), { label: "設定專案預設字幕樣式" })}>設為專案預設<//>`}
    </div>
  <//>`;
}

function PositionField({ project, cap }) {
  const legacy = useStore((s) => legacySize(s.settings));
  const scope = useStore(dragScope);
  const style = resolveStyle(project, cap, legacy);
  const moved = cap.x != null && cap.y != null;
  const pinned = !moved && style.x != null && style.y != null;
  return html`<${Field} label="位置" hint="在預覽畫面按住字幕拖曳，可以移到任何位置">
    <${Segmented} value=${cap.position || "bottom"} options=${POSITIONS} disabled=${moved || pinned}
      onChange=${(position) => updateCaption(cap.id, { position }, "字幕位置")} />
    ${moved && html`<div class="row">
      <span class="insp-hint dim">這句已拖曳到自訂位置</span>
      <span class="grow"></span>
      <${Button} size="sm" kind="ghost" icon="undo" onClick=${() => updateCaption(cap.id, { x: null, y: null }, "重設字幕位置")}>
        重設位置<//>
    </div>`}
    ${pinned && html`<span class="insp-hint dim">跟著樣式「${style.name}」的拖曳位置；在編輯樣式可以回到上中下位置</span>`}
    <div class="row">
      <span class="insp-hint dim">拖曳範圍</span>
      <${Segmented} size="sm" value=${scope} options=${DRAG_SCOPES} onChange=${setDragScope} />
    </div>
  <//>`;
}

function CaptionPanel({ project, cap }) {
  const len = cap.end - cap.start;
  const text = (cap.text || "").trim();
  const glossaryText = useStore((s) => s.settings && s.settings.glossary);
  const glossary = useMemo(() => parseGlossary(glossaryText), [glossaryText]);
  const typos = glossary.length ? glossaryHits(cap.text, glossary) : [];
  const remove = () => {
    closePanel();
    ops([{ op: "caption.delete", id: cap.id }], { label: "刪除字幕" });
  };
  return html`<div class="insp-shot">
    <${Head} icon="captions" title=${text || "（空白字幕）"}
      sub=${html`${fmtSeconds(cap.start, 2)} 到 ${fmtSeconds(cap.end, 2)}<i></i>長 ${fmtSeconds(len, 2)}`} />
    <div class="insp-scroll">
      <${Field} label="字幕文字" hint=${cap.title ? "標題字卡：會燒進匯出的影片，不會寫進 SRT 字幕檔；改顏色按下方「編輯樣式」"
        : "可以換行；空白字幕不會出現在預覽與匯出"}>
        <${DraftText} multiline rows=${3} value=${cap.text || ""} maxlength="500"
          onCommit=${(v) => updateCaption(cap.id, { text: v }, "修改字幕", `caption-text:${cap.id}`)} />
      <//>
      ${typos.length > 0 && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
        <span class="note-text">可能的錯字（比對設定的字幕用語詞庫）：${hitText(typos)}</span>
        <${Button} size="sm" onClick=${() => updateCaption(cap.id, { text: fixHits(cap.text, typos) }, "改正字幕錯字")}>全部改正<//>
      </div>`}
      ${tooLong(cap) && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
        <span class="note-text">字幕偏長：${readChars(cap.text)} 字只顯示 ${fmtSeconds(len, 1)}，以每秒 ${DEFAULT_RATE} 字算最多 ${maxChars(cap)} 字</span>
        <${Button} size="sm" onClick=${() => openCaptionCondense(cap.id)}>濃縮…<//>
      </div>`}
      <${VoiceField} project=${project} cap=${cap} />
      <${ShotField} project=${project} cap=${cap} />
      <div class="row-2">
        <${Field} label="開始">
          <${Seconds} value=${cap.start} max=${cap.end - MIN_LEN} onCommit=${(v) => updateCaption(cap.id, { start: v }, "調整字幕時間")} />
        <//>
        <${Field} label="結束">
          <${Seconds} value=${cap.end} min=${cap.start + MIN_LEN} onCommit=${(v) => updateCaption(cap.id, { end: v }, "調整字幕時間")} />
        <//>
      </div>
      <${StyleField} project=${project} cap=${cap} />
      <${PositionField} project=${project} cap=${cap} />
      <div class="row">
        <${Button} size="sm" icon="skip-back" onClick=${() => setTime(cap.start)}>跳到這裡<//>
        <${Button} size="sm" icon="arrow-right" title="保留長度，把開頭對齊目前播放頭"
          onClick=${() => {
            const start = round(clock.time, 3);
            updateCaption(cap.id, { start, end: round(start + len, 3) }, "移動字幕");
          }}>移到播放頭<//>
      </div>
    </div>
    <div class="insp-foot">
      <span class="insp-hint dim">${clipKeysHint("刪除")}</span>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${remove}>刪除字幕<//>
    </div>
  </div>`;
}

// Audio clips Ctrl+clicked together on the timeline: one volume for all of them, with the same choices as
// the import dialog. The level is absolute (+6dB is the file's own level raised 6 dB), so a clip's volume
// keyframes would fight it and are dropped; one Ctrl+Z undoes the lot.
function AudioBatchPanel({ project, items }) {
  const keyed = items.filter((a) => hasKeys(a, "volume")).length;
  const volumeOff = (a) => (a.keyframes || []).map(({ volume, ...k }) => k).filter((k) => Object.keys(k).some((f) => f !== "t"));
  const apply = (db) => ops(items.map((a) => ({
    op: "audio.update", id: a.id,
    patch: { volume: Math.min(4, dbToGain(db)), ...(hasKeys(a, "volume") ? { keyframes: volumeOff(a) } : {}) },
  })), { label: `${items.length} 段音訊改成 ${db > 0 ? "+" : ""}${db}dB` });
  return html`<div class="insp-shot">
    <${Head} icon="music" title=${`已選 ${items.length} 段音訊`} sub="Ctrl+點擊時間軸上的音訊片段加選或取消" />
    <div class="insp-scroll">
      <${Field} label="音量" hint="以原始檔音量為準，選項與匯入時相同；之後仍可逐段調整">
        <div class="gain-choices">
          ${GAIN_CHOICES.map(([db, label, hint]) => html`<${Button} key=${db} onClick=${() => apply(db)}>
            <b>${label}</b><span class="gain-hint">${hint}</span>
          <//>`)}
        </div>
      <//>
      ${keyed > 0 && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
        <span class="note-text">其中 ${keyed} 段有音量關鍵幀，套用後會清除音量關鍵幀（${undoKeyText()}可還原）。</span></div>`}
      <ul class="batch-list">${items.map((a) => html`<li key=${a.id}>${a.name || (audioMedia(project, a) || {}).name || "音訊"}<span class="dim">
        ${Math.round(Number(valueAt(a, "volume", 0, 1)) * 100)}%${hasKeys(a, "volume") ? "（有關鍵幀）" : ""}</span></li>`)}</ul>
      <${Button} size="sm" onClick=${() => setState({ audioSel: null })}>取消多選<//>
    </div>
  </div>`;
}

export function EditProps({ project, sel }) {
  const together = useStore(multiAudio);
  if (sel && sel.type === "audio") {
    const items = together ? (project.audio || []).filter((a) => together.includes(a.id)) : [];
    if (items.length > 1) return html`<aside class="inspector" aria-label="多段音訊"><${AudioBatchPanel} project=${project} items=${items} /></aside>`;
    const item = (project.audio || []).find((a) => a.id === sel.id);
    if (item) return html`<aside class="inspector" aria-label="音軌屬性"><${AudioPanel} project=${project} item=${item} /></aside>`;
  }
  if (sel && sel.type === "caption") {
    const cap = (project.captions || []).find((c) => c.id === sel.id);
    if (cap) return html`<aside class="inspector" aria-label="字幕屬性"><${CaptionPanel} project=${project} cap=${cap} /></aside>`;
  }
  if (sel && sel.type === "overlay") {
    const item = (project.overlays || []).find((o) => o.id === sel.id);
    if (item) return html`<aside class="inspector" aria-label="片段屬性"><${OverlayPanel} project=${project} item=${item} /></aside>`;
  }
  if (sel && sel.type === "adjust") {
    const item = (project.adjustments || []).find((j) => j.id === sel.id);
    if (item) return html`<aside class="inspector" aria-label="調整圖層屬性"><${GradePanel} project=${project} item=${item} /></aside>`;
  }
  return html`<${Inspector} />`;
}
