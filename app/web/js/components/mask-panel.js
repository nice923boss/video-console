// Blur and mosaic boxes over a shot's picture, to hide a face or a logo (GR-12). Each box is placed with
// sliders as a share of the output frame; while one is dragged the list lives in the store's maskDraft, so the
// preview follows, and letting go saves it as one undo step (as GradeEditor does for grades).
import { html, useEffect } from "../lib/h.js";
import { getState, setState, useStore } from "../lib/store.js";
import { updateShot } from "../lib/actions.js";
import { DEFAULT_MASK, MAX_MASKS } from "../lib/masks.js";
import { Button, Field, IconButton, Segmented, Slider } from "./ui.js";

const MODES = [
  { value: "blur", label: "模糊", icon: "eye-off" },
  { value: "mosaic", label: "馬賽克", icon: "grid" },
];
const pct = (v) => `${Math.round(v * 100)}%`;
const round3 = (v) => Math.round(v * 1000) / 1000;

// The box kept inside the frame after one value changed, as opfields.py _masks keeps it.
function withValue(m, k, v) {
  const next = { ...m, [k]: round3(v) };
  return { ...next, x: Math.min(next.x, round3(1 - next.w)), y: Math.min(next.y, round3(1 - next.h)) };
}

export function MaskEditor({ shot }) {
  const draft = useStore((s) => s.maskDraft);
  const saved = shot.masks || [];
  const masks = draft && draft.id === shot.id ? draft.masks : saved;

  // A panel closed mid-drag must not leave its draft on the preview.
  useEffect(() => () => {
    const d = getState().maskDraft;
    if (d && d.id === shot.id) setState({ maskDraft: null });
  }, [shot.id]);

  const preview = (next) => setState({ maskDraft: { id: shot.id, masks: next } });
  // The draft stays on until the save comes back, so the preview does not blink back to the old boxes.
  const commit = (next, label = "調整遮罩") => {
    if (JSON.stringify(next) === JSON.stringify(saved)) {
      setState({ maskDraft: null });
      return;
    }
    const mine = { id: shot.id, masks: next };
    setState({ maskDraft: mine });
    updateShot(shot.id, { masks: next }, { label }).then(() => {
      if (getState().maskDraft === mine) setState({ maskDraft: null });
    });
  };
  const change = (i, k, v) => masks.map((m, j) => (j === i ? withValue(m, k, v) : m));

  const slider = (i, k, label, min, max) => html`<${Field} label=${label}>
    <${Slider} value=${masks[i][k]} min=${min} max=${max} step=${0.005} format=${pct}
      onChange=${(v) => preview(change(i, k, v))} onCommit=${(v) => commit(change(i, k, v))} />
  <//>`;

  return html`<div class="mask-list">
    ${masks.map((m, i) => html`<div class="mask-card" key=${i}>
      <div class="mask-head">
        <span class="mask-name">遮罩 ${i + 1}</span>
        <${Segmented} size="sm" value=${m.mode} options=${MODES}
          onChange=${(mode) => commit(masks.map((x, j) => (j === i ? { ...x, mode } : x)))} />
        <${IconButton} icon="trash" size=${14} title="刪除這個遮罩"
          onClick=${() => commit(masks.filter((_, j) => j !== i), "刪除遮罩")} />
      </div>
      <div class="row-2">${slider(i, "x", "水平位置", 0, 1 - m.w)}${slider(i, "y", "垂直位置", 0, 1 - m.h)}</div>
      <div class="row-2">${slider(i, "w", "寬度", 0.02, 1)}${slider(i, "h", "高度", 0.02, 1)}</div>
      ${slider(i, "strength", m.mode === "mosaic" ? "格子大小" : "模糊強度", 0.1, 1)}
    </div>`)}
    <${Button} size="sm" icon="plus" disabled=${masks.length >= MAX_MASKS}
      title=${masks.length >= MAX_MASKS ? `每個鏡頭最多 ${MAX_MASKS} 個遮罩` : "在畫面中央加一個模糊框"}
      onClick=${() => commit([...masks, DEFAULT_MASK], "新增遮罩")}>新增遮罩<//>
  </div>`;
}
