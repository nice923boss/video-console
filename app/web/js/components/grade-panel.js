// Property panel of a selected adjustment layer: name and timing, then the colour grade laid out like
// Final Cut Pro's colour wheels (global, shadows, midtones, highlights, each with a hue puck and
// saturation / brightness bars), temperature, tint, hue and mix sliders, and the same values as numbers.
// While a control is dragged the grade lives in the store's gradeDraft, so the preview follows the pointer;
// letting go saves it as one undo step.
import { html, useEffect } from "../lib/h.js";
import { getState, setState, useStore } from "../lib/store.js";
import { ops } from "../lib/actions.js";
import { clock, setTime } from "../lib/clock.js";
import { clamp, fmtSeconds, round } from "../lib/format.js";
import { WHEELS, defaultGrade, fullGrade, isWheelNeutral, neutralWheel, puckOf, withPuck } from "../lib/grade.js";
import { hasKeys, valueAt } from "../lib/keyframes.js";
import { adjustName } from "./tl-adjust.js";
import { KeyList, keyedPatch, useClipTime } from "./key-panel.js";
import { addKeyframe } from "./tl-commands.js";
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

export function GradePanel({ project, item }) {
  const draft = useStore((s) => s.gradeDraft);
  const start = Number(item.start || 0);
  const len = Number(item.length || MIN_LEN);
  const keyed = hasKeys(item, "grade");
  const local = useClipTime(start, len, (item.keyframes || []).length > 0);
  // With grade keyframes the panel shows, and edits, the key at the playhead.
  const saved = fullGrade(keyed ? valueAt(item, "grade", local) : item.grade);
  const grade = draft && draft.id === item.id ? draft.grade : saved;
  const untouched = JSON.stringify(grade) === JSON.stringify(defaultGrade());
  const trackOff = !!(project.tracks || {}).adjustHidden;
  const update = (patch, label) => ops([{ op: "adjust.update", id: item.id, patch }], { label });

  // A panel closed mid-drag must not leave its draft on the preview.
  useEffect(() => () => {
    const d = getState().gradeDraft;
    if (d && d.id === item.id) setState({ gradeDraft: null });
  }, [item.id]);

  const preview = (next) => setState({ gradeDraft: { id: item.id, grade: next } });
  const cancel = () => setState({ gradeDraft: null });
  // The draft stays on until the save comes back, so the preview does not blink back to the old grade.
  const commit = (next, label = "調色") => {
    if (JSON.stringify(next) === JSON.stringify(saved)) {
      cancel();
      return;
    }
    const mine = { id: item.id, grade: next };
    setState({ gradeDraft: mine });
    update(keyedPatch(item, clamp(clock.time - start, 0, len), { grade: next }), label).then(() => {
      if (getState().gradeDraft === mine) setState({ gradeDraft: null });
    });
  };
  const wheelOps = (w) => ({
    onPreview: (wheel) => preview({ ...grade, [w]: wheel }),
    onCommit: (wheel) => commit({ ...grade, [w]: wheel }),
    onCancel: cancel,
  });
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
        <${IconButton} icon="x" size=${16} title="回到鏡頭屬性（Esc）" onClick=${closePanel} />
      </div>
    </header>
    <div class="insp-scroll">
      <div class="note note-info"><${Icon} name="palette" size=${15} />
        <span class="note-text">調整圖層本身沒有畫面。這段時間內的主畫面與上方畫面軌都會套用這組調色，字幕不受影響。</span></div>
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
      <${Field} label="色輪" hint="拖曳圓盤上的圓點調整顏色，左側直條是飽和度，右側直條是亮度；雙擊歸零"
        after=${html`<${Button} size="sm" kind="ghost" icon="undo" disabled=${untouched}
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
      <//>
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
      <span class="insp-hint dim">Delete 刪除，Esc 回到鏡頭屬性</span>
      <span class="grow"></span>
      <${Button} kind="danger" icon="trash" onClick=${remove}>刪除調整圖層<//>
    </div>
  </div>`;
}
