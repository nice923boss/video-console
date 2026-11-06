// Opacity, move, scale, rotate, trim and distort of a shot's picture (ED-30), after the FCPX inspector's
// Compositing, Transform, Crop and Distort. Values show in output-frame pixels (y up, as FCPX) and percent; the
// store keeps fractions of the frame (lib/transform.js). While a slider is dragged the transform lives in the
// store's transformDraft so the preview follows, and letting go saves it as one undo step (as MaskEditor does).
import { html, useEffect } from "../lib/h.js";
import { getState, setState, useStore } from "../lib/store.js";
import { updateShot, whenSaved } from "../lib/actions.js";
import { round } from "../lib/format.js";
import { CORNERS, CROP_MAX, NEUTRAL, fullTransform, isIdentity } from "../lib/transform.js";
import { Badge, IconButton, NumberInput, Section, Slider } from "./ui.js";

const r5 = (v) => Math.round(v * 1e5) / 1e5;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const ZERO_CROP = { l: 0, r: 0, t: 0, b: 0 };
const ZERO_DISTORT = Object.fromEntries(CORNERS.map((k) => [k, [0, 0]]));
const CORNER_LABELS = { tl: "左上", tr: "右上", br: "右下", bl: "左下" };
const MOVE_KEYS = ["x", "y", "rotation", "scale", "scaleX", "scaleY", "anchorX", "anchorY"];

// One value: label, a slider that previews while dragged, and a field for the exact number.
function Row({ label, value, unit, min, max, smin = min, smax = max, step = 1, digits = 0, preview, commit }) {
  return html`<div class="tf-row">
    <span class="tf-label">${label}</span>
    <${Slider} value=${value} min=${Math.min(smin, value)} max=${Math.max(smax, value)} step=${step}
      onChange=${preview} onCommit=${commit} />
    <${NumberInput} class="tf-num" value=${round(value, digits)} min=${min} max=${max} step=${step} digits=${digits}
      suffix=${unit} onCommit=${commit} />
  </div>`;
}

// An X and a Y field side by side.
function Pair({ label, x, y, unit = "px", min, max, digits = 0, onX, onY }) {
  return html`<div class="tf-pair">
    <span class="tf-label">${label}</span>
    <span class="tf-axis">X</span>
    <${NumberInput} class="tf-num" value=${round(x, digits)} min=${min} max=${max} digits=${digits} suffix=${unit} onCommit=${onX} />
    <span class="tf-axis">Y</span>
    <${NumberInput} class="tf-num" value=${round(y, digits)} min=${min} max=${max} digits=${digits} suffix=${unit} onCommit=${onY} />
  </div>`;
}

const reset = (title, onClick, off) => html`<${IconButton} icon="undo" size=${14} title=${title} disabled=${off}
  onClick=${onClick} />`;

export function TransformSections({ shot, project }) {
  const draft = useStore((s) => s.transformDraft);
  const W = Number(project.settings && project.settings.width) || 1920;
  const H = Number(project.settings && project.settings.height) || 1080;
  const saved = fullTransform(shot.transform);
  const t = draft && draft.id === shot.id ? draft.transform : saved;

  // A panel closed mid-drag must not leave its draft on the preview.
  useEffect(() => () => {
    const d = getState().transformDraft;
    if (d && d.id === shot.id) setState({ transformDraft: null });
  }, [shot.id]);

  const preview = (next) => setState({ transformDraft: { id: shot.id, transform: next } });
  // The draft stays on until the saved project lands, so the preview does not blink back to the old picture.
  // Fields nudged with the arrow keys pass a coalesce key, so a run of nudges is one undo step.
  const commit = (next, label, coalesce = null) => {
    if (JSON.stringify(fullTransform(next)) === JSON.stringify(saved)) {
      setState({ transformDraft: null });
      return;
    }
    const mine = { id: shot.id, transform: next };
    setState({ transformDraft: mine });
    updateShot(shot.id, { transform: isIdentity(next) ? null : next }, { label, coalesce }).then(whenSaved).then(() => {
      if (getState().transformDraft === mine) setState({ transformDraft: null });
    });
  };
  const withValues = (patch) => ({ ...t, ...patch });
  // A value shown in panel units, kept as `toStore(v)` under `key`.
  const row = (key, label, toStore, rest) => html`<${Row} label=${label} ...${rest}
    preview=${(v) => preview(withValues({ [key]: r5(toStore(v)) }))}
    commit=${(v) => commit(withValues({ [key]: r5(toStore(v)) }), `修改${label}`, `${shot.id}:transform:${key}`)} />`;

  const sx = t.scale * t.scaleX;
  const sy = t.scale * t.scaleY;
  const setCrop = (k, px) => {
    const v = px / (k === "l" || k === "r" ? W : H);
    const other = { l: "r", r: "l", t: "b", b: "t" }[k];
    return withValues({ crop: { ...t.crop, [k]: r5(clamp(v, 0, CROP_MAX - t.crop[other])) } });
  };
  const cropRow = (k, label) => {
    const size = k === "l" || k === "r" ? W : H;
    return html`<${Row} label=${label} value=${t.crop[k] * size} unit="px" min=${0} max=${Math.floor(size * CROP_MAX)}
      smax=${Math.floor(size * 0.5)} preview=${(v) => preview(setCrop(k, v))}
      commit=${(v) => commit(setCrop(k, v), `裁剪${label}`, `${shot.id}:transform:crop.${k}`)} />`;
  };
  const setCorner = (k, axis, px) => {
    const p = [...t.distort[k]];
    p[axis] = r5(clamp(px / (axis ? H : W), -4, 4));
    return withValues({ distort: { ...t.distort, [k]: p } });
  };
  const corner = (k) => html`<${Pair} label=${CORNER_LABELS[k]} x=${t.distort[k][0] * W} y=${t.distort[k][1] * H}
    min=${-4 * Math.max(W, H)} max=${4 * Math.max(W, H)}
    onX=${(v) => commit(setCorner(k, 0, v), "變形", `${shot.id}:transform:distort.${k}.x`)}
    onY=${(v) => commit(setCorner(k, 1, v), "變形", `${shot.id}:transform:distort.${k}.y`)} />`;

  const moved = MOVE_KEYS.some((k) => t[k] !== NEUTRAL[k]);
  const cropped = Object.values(t.crop).some(Boolean);
  const distorted = CORNERS.some((k) => t.distort[k][0] || t.distort[k][1]);
  const on = html`<${Badge} tone="blue">已調整<//>`;

  return html`
    <${Section} title="複合" icon="layers" collapsible defaultOpen=${true} id="shot-composite"
        actions=${html`${t.opacity < 1 && on}${reset("不透明度回到 100%", () => commit(withValues({ opacity: 1 }), "重設不透明度"), t.opacity === 1)}`}>
      ${row("opacity", "不透明度", (v) => v / 100, { value: t.opacity * 100, unit: "%", min: 0, max: 100 })}
    <//>
    <${Section} title="變換" icon="move" collapsible defaultOpen=${true} id="shot-transform"
        actions=${html`${moved && on}${reset("位置、旋轉、縮放、錨點全部回到原狀",
          () => commit(withValues(Object.fromEntries(MOVE_KEYS.map((k) => [k, NEUTRAL[k]]))), "重設變換"), !moved)}`}>
      ${row("x", "位置 X", (v) => v / W, { value: t.x * W, unit: "px", min: -4 * W, max: 4 * W, smin: -W, smax: W })}
      ${row("y", "位置 Y", (v) => v / H, { value: t.y * H, unit: "px", min: -4 * H, max: 4 * H, smin: -H, smax: H })}
      ${row("rotation", "旋轉", (v) => v, { value: t.rotation, unit: "°", min: -3600, max: 3600, smin: -180, smax: 180,
        step: 0.5, digits: 1 })}
      ${row("scale", "縮放", (v) => v / 100, { value: t.scale * 100, unit: "%", min: 1, max: 1000, smin: 1, smax: 400,
        digits: 1 })}
      <${Pair} label="縮放 X／Y" x=${sx * 100} y=${sy * 100} unit="%" min=${1} max=${10000} digits=${1}
        onX=${(v) => commit(withValues({ scaleX: r5(clamp(v / 100 / t.scale, 0.01, 10)) }), "修改縮放 X", `${shot.id}:transform:scaleX`)}
        onY=${(v) => commit(withValues({ scaleY: r5(clamp(v / 100 / t.scale, 0.01, 10)) }), "修改縮放 Y", `${shot.id}:transform:scaleY`)} />
      <${Pair} label="錨點" x=${t.anchorX * W} y=${t.anchorY * H} min=${-4 * Math.max(W, H)} max=${4 * Math.max(W, H)}
        onX=${(v) => commit(withValues({ anchorX: r5(clamp(v / W, -4, 4)) }), "修改錨點", `${shot.id}:transform:anchorX`)}
        onY=${(v) => commit(withValues({ anchorY: r5(clamp(v / H, -4, 4)) }), "修改錨點", `${shot.id}:transform:anchorY`)} />
      <p class="tf-note">位置以畫面中央為原點，往右、往上為正；旋轉逆時針為正；縮放與旋轉繞著錨點（預設畫面中央）。</p>
    <//>
    <${Section} title="裁剪" icon="maximize" collapsible defaultOpen=${false} id="shot-crop"
        actions=${html`${cropped && on}${reset("取消裁剪", () => commit(withValues({ crop: ZERO_CROP }), "重設裁剪"), !cropped)}`}>
      ${cropRow("l", "左")}${cropRow("r", "右")}${cropRow("t", "上")}${cropRow("b", "下")}
      <p class="tf-note">修剪掉的邊變成黑色，畫面不會放大補滿；要補滿請再調大縮放。</p>
    <//>
    <${Section} title="變形" icon="grid" collapsible defaultOpen=${false} id="shot-distort"
        actions=${html`${distorted && on}${reset("四個角回到原位", () => commit(withValues({ distort: ZERO_DISTORT }), "重設變形"), !distorted)}`}>
      ${CORNERS.map(corner)}
      <p class="tf-note">移動畫面四個角，做出透視或梯形；往右、往上為正，先變形再套用上面的變換。</p>
    <//>`;
}
