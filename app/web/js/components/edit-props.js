// Right-hand panel of the edit view. A selected soundtrack item, caption, picture-in-picture clip or
// adjustment layer gets its own property panel; anything else falls back to the shot inspector.
import { html } from "../lib/h.js";
import { setState, useStore } from "../lib/store.js";
import { ops } from "../lib/actions.js";
import { clock, setTime } from "../lib/clock.js";
import { clamp, fmtSeconds, round, shotLabel } from "../lib/format.js";
import { audioLength, audioMedia, overlayHeight, fitFrame, buildSequence, clipIndexAt } from "../lib/sequence.js";
import { hasKeys, valueAt } from "../lib/keyframes.js";
import { VOICE_STATES, voiceState, voiceClip, generateVoices } from "../lib/voice.js";
import { openCastDialog } from "../dialogs/cast-dialog.js";
import { openCaptionStyleDialog } from "../dialogs/caption-style-dialog.js";
import {
  legacySize, styleById, defaultStyleId, resolveStyle, dragScope, setDragScope, setDefaultOps, useForAllOps,
} from "../lib/caption-style.js";
import { Button, IconButton, Field, DraftText, NumberInput, Slider, Segmented, Toggle, Select, Badge } from "./ui.js";
import { Icon } from "./icons.js";
import { Inspector } from "./inspector.js";
import { GradePanel } from "./grade-panel.js";
import { KeyList, keyedPatch, useClipTime } from "./key-panel.js";
import { addKeyframe } from "./tl-commands.js";

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
      <${IconButton} icon="x" size=${16} title="回到鏡頭屬性（Esc）" onClick=${closePanel} />
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

  return html`<div class="insp-shot">
    <${Head} icon=${item.muted ? "volume-x" : "music"} title=${name}
      sub=${html`開始 ${fmtSeconds(start, 2)}<i></i>長 ${fmtSeconds(len, 2)}${item.muted ? html`<i></i><span class="warn-text">靜音</span>` : ""}`} />
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
      <span class="insp-hint dim">Delete 移除，Esc 回到鏡頭屬性</span>
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
    set({ x: round(clamp(x, -1, 1), 4), y: round(clamp(y, -1, 1), 4), ...(size ? { w: size } : {}) }, "片段位置");
  const spots = [
    { label: "滿版", ...fitFrame(media, fw, fh) },
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
        : "以畫面左上角為起點；也可以直接套用滿版或角落位置"}>
        <div class="row">
          ${spots.map((s) => html`<${Button} size="sm" kind="ghost" onClick=${() => place(s)}>${s.label}<//>`)}
        </div>
      <//>
      <div class="row-2">
        <${Field} label="水平">
          <${Slider} value=${at("x", 0)} min=${-1} max=${1} step=${0.005} format=${pct}
            onCommit=${(v) => set({ x: round(v, 4) }, "片段位置")} />
        <//>
        <${Field} label="垂直">
          <${Slider} value=${at("y", 0)} min=${-1} max=${1} step=${0.005} format=${pct}
            onCommit=${(v) => set({ y: round(v, 4) }, "片段位置")} />
        <//>
      </div>
      <${Field} label="大小" hint="畫面寬度的百分比，高度依素材比例">
        <${Slider} value=${w} min=${0.05} max=${1} step=${0.01} format=${pct}
          onCommit=${(v) => set({ w: round(v, 4) }, "片段大小")} />
      <//>
      <${Field} label="不透明度">
        <${Slider} value=${at("opacity", 1)} min=${0} max=${1} step=${0.01} format=${pct}
          onCommit=${(v) => set({ opacity: round(v, 2) }, "片段不透明度")} />
      <//>
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
      <span class="insp-hint dim">Delete 移除，Esc 回到鏡頭屬性</span>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${remove}>移除片段<//>
    </div>
  </div>`;
}

function VoiceField({ project, cap }) {
  const jobs = useStore((s) => s.jobs);
  const cast = project.cast || [];
  const state = voiceState(project, cap, jobs);
  const info = VOICE_STATES[state];
  const clip = voiceClip(project, cap.id);
  const clipLen = clip ? audioLength(clip, (project.media || {})[clip.mediaId]) : 0;
  const over = clipLen - (cap.end - cap.start);
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
  const bind = (shot) => {
    const others = shot ? (project.captions || []).filter((c) => c.shot === shot && c.id !== cap.id) : [];
    ops([{ op: "caption.update", id: cap.id, patch: { shot } },
      ...others.map((c) => ({ op: "caption.update", id: c.id, patch: { shot: null } }))],
    { label: shot ? "綁定鏡頭" : "解除綁定鏡頭" });
  };
  return html`<${Field} label="對應鏡頭" hint="綁定後分鏡卡會顯示這句字幕，兩邊改文字會同步；時間與長度不受影響，一個鏡頭只對應一句">
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
  const remove = () => {
    closePanel();
    ops([{ op: "caption.delete", id: cap.id }], { label: "刪除字幕" });
  };
  return html`<div class="insp-shot">
    <${Head} icon="captions" title=${text || "（空白字幕）"}
      sub=${html`${fmtSeconds(cap.start, 2)} 到 ${fmtSeconds(cap.end, 2)}<i></i>長 ${fmtSeconds(len, 2)}`} />
    <div class="insp-scroll">
      <${Field} label="字幕文字" hint="可以換行；空白字幕不會出現在預覽與匯出">
        <${DraftText} multiline rows=${3} value=${cap.text || ""} maxlength="500"
          onCommit=${(v) => updateCaption(cap.id, { text: v }, "修改字幕", `caption-text:${cap.id}`)} />
      <//>
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
      <span class="insp-hint dim">Delete 刪除，Esc 回到鏡頭屬性</span>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${remove}>刪除字幕<//>
    </div>
  </div>`;
}

export function EditProps({ project, sel }) {
  if (sel && sel.type === "audio") {
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
