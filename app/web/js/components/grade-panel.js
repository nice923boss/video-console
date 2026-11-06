// Property panel of a selected adjustment layer: name and timing, then the colour grade laid out like
// Final Cut Pro's colour wheels (global, shadows, midtones, highlights, each with a hue puck and
// saturation / brightness bars), temperature, tint, hue and mix sliders, and the same values as numbers.
// While a control is dragged the grade lives in the store's gradeDraft, so the preview follows the pointer;
// letting go saves it as one undo step. GradeEditor, the colour controls alone, also grades a single shot
// from its inspector (GR-01). Its 白平衡 section is the white balance block (grade.wb) that runs before the
// wheels: a sampled grey, exposure, colour and tone sliders, and automatic balance / exposure read from the
// frame at the playhead (POST /api/projects/{pid}/analyze).
import { html, useEffect, useState } from "../lib/h.js";
import { getState, setState, useStore } from "../lib/store.js";
import { ops, whenSaved } from "../lib/actions.js";
import { clock, setTime } from "../lib/clock.js";
import { clamp, fmtSeconds, round } from "../lib/format.js";
import { NEUTRAL_WB, WHEELS, defaultGrade, fullGrade, isWheelNeutral, neutralWheel, puckOf, withPuck, wbNeutral } from "../lib/grade.js";
import { api } from "../lib/api.js";
import { toast, toastError } from "./toast.js";
import { hasKeys, valueAt } from "../lib/keyframes.js";
import { adjustName } from "./tl-adjust.js";
import { KeyList, keyedPatch, useClipTime } from "./key-panel.js";
import { addKeyframe } from "./tl-commands.js";
import { clipKeysHint, keyHint } from "../lib/keymap.js";
import { Button, IconButton, Field, DraftText, NumberInput, Slider, Toggle } from "./ui.js";
import { Icon } from "./icons.js";

const MAX_TIME = 36000;
const MIN_LEN = 0.1;
const GRAB = 9; // px: a press this close to the puck or a bar's knob drags it from where it is
const WHEEL_NAMES = { global: "全局", shadows: "陰影", midtones: "中間調", highlights: "高光" };

const signed = (v, digits = 2) => `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;
const closePanel = () => setState({ editSel: null });

const REST = [
  { k: "temp", label: "色溫", min: 2500, max: 10000, step: 10, digits: 1, format: (v) => v.toFixed(1),
    hint: "5000 為原色，數值越高越暖（偏橘），越低越冷（偏藍）" },
  { k: "tint", label: "色調", min: -100, max: 100, step: 1, digits: 1, format: (v) => v.toFixed(0),
    hint: "正值偏洋紅，負值偏綠" },
  { k: "hue", label: "色相", min: -180, max: 180, step: 1, digits: 1, format: (v) => `${v.toFixed(0)}°`,
    hint: "所有顏色沿色環一起旋轉" },
  { k: "mix", label: "混合", min: 0, max: 1, step: 0.001, digits: 3, format: (v) => `${(v * 100).toFixed(1)}%`,
    hint: "100% 完整套用，降低時與原始畫面混合" },
];

const pct = (v) => `${(v * 100).toFixed(0)}%`;
const WB_GROUPS = [
  { title: "平衡", fields: [
    { k: "amount", label: "平衡強度", min: 0, max: 1, step: 0.01, format: pct,
      hint: "100% 讓取樣色變成中性灰，0 不校正" },
    { k: "exposure", label: "取樣曝光", min: 0, max: 1, step: 0.01, format: pct,
      hint: "把取樣點的亮度拉向目標亮度，0 不改亮度" },
    { k: "level", label: "目標亮度", min: 0.05, max: 0.95, step: 0.01, format: (v) => v.toFixed(2),
      hint: "取樣曝光要拉到的亮度，0.5 為中灰" },
  ] },
  { title: "色彩", fields: [
    { k: "temp", label: "色溫", min: -100, max: 100, step: 1, format: (v) => signed(v, 0), hint: "正值偏暖，負值偏冷" },
    { k: "tint", label: "色調", min: -100, max: 100, step: 1, format: (v) => signed(v, 0), hint: "正值偏洋紅，負值偏綠" },
    { k: "hue", label: "色相", min: -180, max: 180, step: 1, format: (v) => `${v.toFixed(0)}°`, hint: "所有顏色沿色環旋轉" },
    { k: "sat", label: "飽和度", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "-1 為黑白" },
  ] },
  { title: "亮度與對比", fields: [
    { k: "blacks", label: "黑位", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "正值把最暗處壓到全黑" },
    { k: "lift", label: "暗部提升", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "提亮或壓暗暗部，亮部幾乎不動" },
    { k: "gain", label: "亮部增益", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "整體乘上增益，亮部變化最大" },
    { k: "gamma", label: "中間調", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "正值提亮中間調，黑白兩端不動" },
    { k: "fill", label: "補光", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "只提亮陰影，像補一盞燈" },
    { k: "bright", label: "亮度", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "整體加減亮度" },
    { k: "contrast", label: "對比", min: -1, max: 1, step: 0.01, format: (v) => signed(v), hint: "以中灰為軸拉開或收攏" },
  ] },
  { title: "套用範圍", fields: [
    { k: "shadows", label: "陰影", min: 0, max: 1, step: 0.01, format: pct, hint: "白平衡套用在暗部的比例" },
    { k: "midtones", label: "中間調", min: 0, max: 1, step: 0.01, format: pct, hint: "白平衡套用在中間調的比例" },
    { k: "highlights", label: "高光", min: 0, max: 1, step: 0.01, format: pct, hint: "白平衡套用在亮部的比例" },
    { k: "mix", label: "混合", min: 0, max: 1, step: 0.01, format: pct, hint: "與原始畫面混合" },
  ] },
];
const WB_AUTO = {
  both: { wb: true, exposure: true, label: "自動白平衡與曝光" },
  wb: { wb: true, exposure: false, label: "自動白平衡" },
  exposure: { wb: false, exposure: true, label: "自動曝光" },
  sample: { wb: true, exposure: false, label: "依取樣點平衡", strength: 1 },
};
const rgbCss = (wb) => `rgb(${[wb.r, wb.g, wb.b].map((v) => Math.round(v * 255)).join(",")})`;

// The white balance block: sliders for grade.wb, and automatic balance / exposure from the frame `probe()`
// names (an analyze body without the options, or null when there is no picture to read).
function WhiteBalance({ wb, probe, onPreview, onCommit }) {
  const [busy, setBusy] = useState("");
  const set = (k, v) => ({ ...wb, [k]: v });
  const auto = async (mode) => {
    const at = probe && probe();
    const pid = getState().project?.id;
    if (!at || !pid) {
      toast("播放頭這裡沒有畫面可以分析", { level: "warn" });
      return;
    }
    const opt = WB_AUTO[mode];
    const body = { ...at, wb: opt.wb, exposure: opt.exposure, current: wb };
    if (opt.strength != null) body.strength = opt.strength;
    if (mode === "sample") Object.assign(body, { x: wb.x, y: wb.y, size: wb.size });
    setBusy(mode);
    try {
      const res = await api.post(`/api/projects/${pid}/analyze`, body);
      onCommit({ ...res.suggest }, opt.label);
      const st = res.stats;
      const d = res.decision || {};
      const read = d.subject === "person"
        ? `以人物測光，臉部亮度 ${d.face.toFixed(2)}`
        : `畫面亮度中位 ${st.p50.toFixed(2)}，過曝 ${(st.clipHigh * 100).toFixed(1)}%`;
      toast(`${opt.label}：${read}`, { duration: 4000 });
    } catch (err) {
      toastError(err, opt.label);
    } finally {
      setBusy("");
    }
  };
  const num = (k, label, min, max) => html`<${Field} label=${label}>
    <${NumberInput} value=${wb[k]} min=${min} max=${max} step=${0.01} digits=${3} onCommit=${(v) => onCommit(set(k, v))} />
  <//>`;
  const slider = (f) => html`<${Field} key=${f.k} label=${f.label} hint=${f.hint}
      after=${wb[f.k] !== NEUTRAL_WB[f.k] && html`<${IconButton} icon="undo" size=${13} title=${`重設${f.label}`}
        onClick=${() => onCommit(set(f.k, NEUTRAL_WB[f.k]), `重設白平衡${f.label}`)} />`}>
    <${Slider} value=${wb[f.k]} min=${f.min} max=${f.max} step=${f.step} format=${f.format}
      onChange=${(v) => onPreview(set(f.k, round(v, 3)))} onCommit=${(v) => onCommit(set(f.k, round(v, 3)))} />
  <//>`;
  const changed = (g) => g.fields.some((f) => wb[f.k] !== NEUTRAL_WB[f.k]);
  const sampled = ["r", "g", "b"].some((k) => wb[k] !== NEUTRAL_WB[k]);
  return html`<${Field} label="白平衡" hint="先於色輪套用。取樣點填畫面上應該是灰或白的位置；自動功能讀播放頭這一格"
      after=${html`<${Button} size="sm" kind="ghost" icon="undo" disabled=${wbNeutral(wb)}
        onClick=${() => onCommit({ ...NEUTRAL_WB }, "重設白平衡")}>重設<//>`}>
    <div class="row">
      ${["both", "wb", "exposure"].map((m) => html`<${Button} key=${m} size="sm" disabled=${!!busy || !probe}
        onClick=${() => auto(m)}>${busy === m ? "分析中…" : WB_AUTO[m].label}<//>`)}
    </div>
    <div class="grade-secs">
      <details class="grade-sec">
        <summary>取樣點${sampled && html`<span class="grade-dot" title="已取樣"></span>`}</summary>
        <div class="grade-sec-body">
          <div class="row-3">${num("x", "X", 0, 1)}${num("y", "Y", 0, 1)}${num("size", "大小", 0.01, 1)}</div>
          <div class="wb-sample">
            <span class="wb-swatch" style=${`background:${rgbCss(wb)}`} title="取樣色"></span>
            <div class="row-3">${num("r", "R", 0.01, 1)}${num("g", "G", 0.01, 1)}${num("b", "B", 0.01, 1)}</div>
          </div>
          <${Button} size="sm" disabled=${!!busy || !probe} onClick=${() => auto("sample")}
            title="讀播放頭這一格在 X、Y 位置、邊長為畫面寬度乘大小的方塊，把它的平均色當成灰色">${busy === "sample" ? "分析中…" : "依取樣點平衡"}<//>
        </div>
      </details>
      ${WB_GROUPS.map((g) => html`<details key=${g.title} class="grade-sec">
        <summary>${g.title}${changed(g) && html`<span class="grade-dot" title="已調整"></span>`}</summary>
        <div class="grade-sec-body">${g.fields.map(slider)}</div>
      </details>`)}
    </div>
  <//>`;
}

function Seconds({ value, onCommit, min = 0 }) {
  return html`<${NumberInput} value=${value} onCommit=${onCommit} min=${min} max=${MAX_TIME} step=${0.1} digits=${3} suffix="秒" />`;
}

// Follows one press on e.currentTarget until it is let go: onMove(x, y) in the element's own pixels,
// onEnd(cancelled).
function follow(e, onMove, onEnd) {
  const el = e.currentTarget;
  const box = el.getBoundingClientRect();
  const at = (ev) => onMove(ev.clientX - box.left, ev.clientY - box.top);
  const stop = (ev) => {
    el.removeEventListener("pointermove", at);
    el.removeEventListener("pointerup", stop);
    el.removeEventListener("pointercancel", stop);
    onEnd(ev.type === "pointercancel");
  };
  e.preventDefault();
  el.setPointerCapture(e.pointerId);
  el.addEventListener("pointermove", at);
  el.addEventListener("pointerup", stop);
  el.addEventListener("pointercancel", stop);
  at(e);
}

// A vertical bar for one wheel's saturation or brightness: top is max, the middle tick the neutral value.
function Bar({ label, value, min, max, neutral, format, cls, onPreview, onCommit, onCancel }) {
  const pos = (v) => 1 - (v - min) / (max - min); // 0 at the top, 1 at the bottom
  const down = (e) => {
    const h = e.currentTarget.clientHeight;
    let start = null;
    let last = null;
    follow(e, (x, y) => {
      // Grabbing the knob keeps it under the pointer; pressing elsewhere jumps there first.
      if (start == null) start = Math.abs(y - pos(value) * h) <= GRAB ? { y, v: value } : { y, v: min + (1 - y / h) * (max - min) };
      const v = round(clamp(start.v - ((y - start.y) / h) * (max - min), min, max), 3);
      if (v !== (last ?? value)) {
        last = v;
        onPreview(v);
      }
    }, (cancelled) => {
      if (last == null) return;
      if (cancelled) onCancel();
      else onCommit(last);
    });
  };
  const key = (e) => {
    const step = (max - min) / (e.shiftKey ? 20 : 100);
    const dir = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[e.key];
    if (!dir) return;
    e.preventDefault();
    onCommit(round(clamp(value + dir * step, min, max), 3));
  };
  return html`<div class=${`cw-bar ${cls}`} role="slider" tabindex="0" aria-label=${label} aria-orientation="vertical"
      aria-valuemin=${min} aria-valuemax=${max} aria-valuenow=${value} aria-valuetext=${format(value)}
      title=${`${label} ${format(value)}\n上下拖曳調整，雙擊歸零`}
      onPointerDown=${down} onKeyDown=${key} onDblClick=${() => value !== neutral && onCommit(neutral)}>
    <span class="cw-knob" style=${`top:${pos(value) * 100}%`}></span>
  </div>`;
}

function ColorWheel({ name, wheel, onPreview, onCommit, onCancel }) {
  const { angle, amount } = puckOf(wheel);
  const rad = (angle * Math.PI) / 180;
  const px = 50 + 50 * amount * Math.sin(rad);
  const py = 50 - 50 * amount * Math.cos(rad);
  const down = (e) => {
    const r = e.currentTarget.clientWidth / 2;
    let start = null;
    let last = null;
    follow(e, (x, y) => {
      // Grabbing the puck moves it by the pointer's travel; pressing elsewhere puts it under the pointer.
      if (start == null) {
        const puck = { x: (px / 100) * 2 * r, y: (py / 100) * 2 * r };
        start = { x, y, from: Math.hypot(x - puck.x, y - puck.y) <= GRAB ? puck : { x, y } };
      }
      const dx = start.from.x + (x - start.x) - r;
      const dy = start.from.y + (y - start.y) - r;
      const next = withPuck(wheel, (Math.atan2(dx, -dy) * 180) / Math.PI, Math.min(1, Math.hypot(dx, dy) / r));
      if (["r", "g", "b"].some((k) => next[k] !== (last || wheel)[k])) {
        last = next;
        onPreview(next);
      }
    }, (cancelled) => {
      if (!last) return;
      if (cancelled) onCancel();
      else onCommit(last);
    });
  };
  const centre = () => {
    if (wheel.r || wheel.g || wheel.b) onCommit({ ...wheel, r: 0, g: 0, b: 0 });
  };
  const bar = (k, props) => html`<${Bar} value=${wheel[k]} cls=${`cw-${k}`} ...${props}
    onPreview=${(v) => onPreview({ ...wheel, [k]: v })} onCommit=${(v) => onCommit({ ...wheel, [k]: v })} onCancel=${onCancel} />`;
  return html`<div class="cw">
    <div class="cw-top">
      <span class="cw-name">${name}</span>
      <${IconButton} icon="undo" size=${13} title=${`重設${name}`} disabled=${isWheelNeutral(wheel)}
        onClick=${() => onCommit(neutralWheel())} />
    </div>
    <div class="cw-body">
      ${bar("sat", { label: `${name}飽和度`, min: 0, max: 2, neutral: 1, format: (v) => v.toFixed(2) })}
      <div class="cw-disc" role="img" aria-label=${`${name}色輪`} onPointerDown=${down} onDblClick=${centre}
        title=${`${name}色輪：拖曳圓點調整顏色，越往外越強；雙擊歸零`}>
        <span class="cw-puck" style=${`left:${px}%;top:${py}%;background:hsl(${angle}deg ${Math.round(amount * 100)}% 50%)`}></span>
      </div>
      ${bar("bright", { label: `${name}亮度`, min: -1, max: 1, neutral: 0, format: (v) => signed(v) })}
    </div>
  </div>`;
}

function RangeNumbers({ name, wheel, onCommit }) {
  const num = (k, label, min, max) => html`<${Field} label=${label}>
    <${NumberInput} value=${wheel[k]} min=${min} max=${max} step=${0.01} digits=${3}
      onCommit=${(v) => onCommit({ ...wheel, [k]: v })} />
  <//>`;
  return html`<details class="grade-sec">
    <summary>${name}${!isWheelNeutral(wheel) && html`<span class="grade-dot" title="已調整"></span>`}</summary>
    <div class="grade-sec-body">
      <span class="field-label">顏色</span>
      <div class="row-3">${num("r", "R", -1, 1)}${num("g", "G", -1, 1)}${num("b", "B", -1, 1)}</div>
      <div class="row-2">${num("sat", "飽和度", 0, 2)}${num("bright", "亮度", -1, 1)}</div>
    </div>
  </details>`;
}

// Wheels, sliders and numbers for the grade saved on `id` (an adjustment layer or a shot). Dragging puts the
// grade in the store's gradeDraft under that id; onSave(next, label) stores it and returns a promise.
// probe() gives the analyze body for the frame the automatic white balance reads (null: none).
export function GradeEditor({ id, saved, onSave, probe = null }) {
  const draft = useStore((s) => s.gradeDraft);
  const compare = useStore((s) => s.gradeCompare);
  const grade = draft && draft.id === id ? draft.grade : saved;
  const untouched = JSON.stringify(grade) === JSON.stringify(defaultGrade());

  // A panel closed mid-drag must not leave its draft on the preview.
  useEffect(() => () => {
    const d = getState().gradeDraft;
    if (d && d.id === id) setState({ gradeDraft: null });
  }, [id]);

  const preview = (next) => setState({ gradeDraft: { id, grade: next } });
  const cancel = () => setState({ gradeDraft: null });
  // The draft stays on until the saved project lands, so the preview does not blink back to the old grade.
  const commit = (next, label = "調色") => {
    if (JSON.stringify(next) === JSON.stringify(saved)) {
      cancel();
      return;
    }
    const mine = { id, grade: next };
    setState({ gradeDraft: mine });
    onSave(next, label).then(whenSaved).then(() => {
      if (getState().gradeDraft === mine) setState({ gradeDraft: null });
    });
  };
  const wheelOps = (w) => ({
    onPreview: (wheel) => preview({ ...grade, [w]: wheel }),
    onCommit: (wheel) => commit({ ...grade, [w]: wheel }),
    onCancel: cancel,
  });

  return html`<${WhiteBalance} wb=${grade.wb} probe=${probe}
      onPreview=${(wb) => preview({ ...grade, wb })} onCommit=${(wb, label = "白平衡") => commit({ ...grade, wb }, label)} />
    <${Field} label="色輪" hint="拖曳圓盤上的圓點調整顏色，左側直條是飽和度，右側直條是亮度；雙擊歸零"
      after=${html`<${IconButton} icon="palette" size=${14} active=${compare}
          title=${compare ? "關閉調色前後對照" : "調色前後對照：預覽左半顯示調色前、右半調色後"}
          onClick=${() => setState({ gradeCompare: !compare })} />
        <${Button} size="sm" kind="ghost" icon="undo" disabled=${untouched}
          onClick=${() => commit(defaultGrade(), "重設調色")}>全部重設<//>`}>
      <div class="grade-wheels">
        ${WHEELS.map((w) => html`<${ColorWheel} key=${w} name=${WHEEL_NAMES[w]} wheel=${grade[w]} ...${wheelOps(w)} />`)}
      </div>
    <//>
    ${REST.map((r) => html`<${Field} key=${r.k} label=${r.label} hint=${r.hint}
      after=${grade[r.k] !== defaultGrade()[r.k] && html`<${IconButton} icon="undo" size=${13} title=${`重設${r.label}`}
        onClick=${() => commit({ ...grade, [r.k]: defaultGrade()[r.k] })} />`}>
      <${Slider} value=${grade[r.k]} min=${r.min} max=${r.max} step=${r.step} format=${r.format}
        onChange=${(v) => preview({ ...grade, [r.k]: round(v, r.digits) })}
        onCommit=${(v) => commit({ ...grade, [r.k]: round(v, r.digits) })} />
    <//>`)}
    <${Field} label="數值" hint="直接輸入色輪的數值；R/G/B 與亮度範圍 -1 到 1，飽和度 0 到 2（1 為原色）">
      <div class="grade-secs">
        ${WHEELS.map((w) => html`<${RangeNumbers} key=${w} name=${WHEEL_NAMES[w]} wheel=${grade[w]}
          onCommit=${(wheel) => commit({ ...grade, [w]: wheel })} />`)}
      </div>
    <//>`;
}

export function GradePanel({ project, item }) {
  const start = Number(item.start || 0);
  const len = Number(item.length || MIN_LEN);
  const keyed = hasKeys(item, "grade");
  const local = useClipTime(start, len, (item.keyframes || []).length > 0);
  // With grade keyframes the panel shows, and edits, the key at the playhead.
  const saved = fullGrade(keyed ? valueAt(item, "grade", local) : item.grade);
  const trackOff = !!(project.tracks || {}).adjustHidden;
  const update = (patch, label) => ops([{ op: "adjust.update", id: item.id, patch }], { label });
  const save = (next, label) => update(keyedPatch(item, clamp(clock.time - start, 0, len), { grade: next }), label);
  const remove = () => {
    closePanel();
    ops([{ op: "adjust.delete", id: item.id }], { label: "刪除調整圖層" });
  };

  return html`<div class="insp-shot">
    <header class="insp-head">
      <div class="insp-icon"><${Icon} name="palette" size=${22} /></div>
      <div class="insp-title">
        <h2 title=${adjustName(item)}>${adjustName(item)}</h2>
        <span class="insp-sub">開始 ${fmtSeconds(start, 2)}<i></i>長 ${fmtSeconds(len, 2)}${item.hidden
          ? html`<i></i><span class="warn-text">已停用</span>` : ""}</span>
      </div>
      <div class="insp-nav">
        <${IconButton} icon="x" size=${16} title=${`回到鏡頭屬性${keyHint("edit.deselect")}`} onClick=${closePanel} />
      </div>
    </header>
    <div class="insp-scroll">
      <div class="note note-info"><${Icon} name="palette" size=${15} />
        <span class="note-text">調整圖層本身沒有畫面。這段時間內的主畫面與上方畫面軌都會套用這組調色。</span></div>
      <${Toggle} checked=${!!project.settings.gradeCaptions} label="字幕也套用調色"
        hint="整個專案的設定，所有調整圖層都適用。關閉時字幕維持原色，例如整片褪色時字幕仍清楚"
        onChange=${(gradeCaptions) => ops([{ op: "project.update", patch: { settings: { gradeCaptions } } }],
          { label: gradeCaptions ? "字幕套用調色" : "字幕不套用調色" })} />
      ${trackOff && html`<div class="note note-warn"><${Icon} name="eye-off" size=${15} />
        <span class="note-text">調整圖層軌已隱藏，預覽與匯出都不會套用。按時間軸「調整圖層」軌名稱旁的眼睛可以重新顯示。</span></div>`}
      <${Field} label="名稱">
        <${DraftText} value=${item.name || ""} placeholder="調整圖層" maxlength="40"
          onCommit=${(v) => update({ name: v.trim() }, "調整圖層名稱")} />
      <//>
      <div class="row-2">
        <${Field} label="開始">
          <${Seconds} value=${start} onCommit=${(v) => update({ start: v }, "移動調整圖層")} />
        <//>
        <${Field} label="長度">
          <${Seconds} value=${len} min=${MIN_LEN} onCommit=${(v) => update({ length: v }, "修改調整圖層長度")} />
        <//>
      </div>
      ${keyed && html`<div class="note note-info"><${Icon} name="diamond" size=${15} />
        <span class="note-text">調色有關鍵幀：這裡顯示並改寫播放頭位置的調色。預覽與輸出在關鍵幀之間分段漸變。</span></div>`}
      <${GradeEditor} id=${item.id} saved=${saved} onSave=${save}
        probe=${() => ({ at: clamp(clock.time, start, start + len - 0.01), basis: "graded" })} />
      <${Toggle} checked=${!!item.hidden} label="停用調整圖層" hint="保留在時間軸上，但預覽與匯出都不套用調色"
        onChange=${(hidden) => update({ hidden }, hidden ? "停用調整圖層" : "啟用調整圖層")} />
      <${KeyList} item=${item} start=${start} length=${len} local=${local} update=${update}
        onAdd=${() => addKeyframe(project, null, { type: "adjust", id: item.id })} />
      <div class="row">
        <${Button} size="sm" icon="skip-back" onClick=${() => setTime(start)}>跳到開頭<//>
        <${Button} size="sm" icon="arrow-right" onClick=${() => update({ start: round(clock.time, 3) }, "移動調整圖層")}
          title="把這個調整圖層的開頭對齊目前播放頭">移到播放頭<//>
      </div>
    </div>
    <div class="insp-foot">
      <span class="insp-hint dim">${clipKeysHint("刪除")}</span>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${remove}>刪除調整圖層<//>
    </div>
  </div>`;
}
