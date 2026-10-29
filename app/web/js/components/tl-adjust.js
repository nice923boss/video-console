// The adjustment-layer lane of the edit timeline and the colour-wheel button on the picture lane's label
// that adds one. An adjustment layer has no picture of its own: its colour grade applies to everything
// under it (the picture lane and the upper picture tracks) for as long as it lasts. Clicking the button
// covers the clip under the playhead; dragging it onto the timeline covers the clip it is let go over.
import { html, useState } from "../lib/h.js";
import { setState } from "../lib/store.js";
import { ops } from "../lib/actions.js";
import { clock, setTime } from "../lib/clock.js";
import { fmtSeconds, newId, round, clamp } from "../lib/format.js";
import { clipIndexAt } from "../lib/sequence.js";
import { defaultGrade, isNeutral } from "../lib/grade.js";
import { shiftKeys, keyPatch } from "../lib/keyframes.js";
import { Icon } from "./icons.js";
import { dragPointer, snapRange, Tip, Handles, KeyMarks, clipActions } from "./tl-tracks.js";

export const ADJUST_H = 30;

const MIN_ITEM = 0.1;
const MAX_TIME = 36000;
const DEFAULT_LEN = 5;

export const adjustName = (item) => item.name || "調整圖層";

// What a new adjustment layer at time t covers: the picture clip there, else DEFAULT_LEN seconds from t.
function spanAt(clips, t) {
  const clip = clips[clipIndexAt(clips, t)];
  if (clip && t < clip.start + clip.length) return { start: round(clip.start, 3), length: round(clip.length, 3) };
  return { start: round(t, 3), length: DEFAULT_LEN };
}

function addAdjustment(span) {
  const id = newId("adj");
  const item = { id, name: "調整圖層", ...span, hidden: false, grade: defaultGrade() };
  ops([{ op: "adjust.add", item }], { label: "新增調整圖層" }).then((res) => {
    if (res) setState({ editSel: { type: "adjust", id } });
  });
}

// onGhost({start, length} | null) shows the span a drag would cover while it is over a lane.
export function AdjustAddButton({ clips, pps, onGhost }) {
  const down = (e) => {
    let span = null;
    dragPointer(e, {
      axis: "xy",
      onMove: (dx, ev) => {
        const lane = document.elementsFromPoint(ev.clientX, ev.clientY).find((el) => el.classList.contains("tl-lane"));
        span = lane ? spanAt(clips, Math.max(0, (ev.clientX - lane.getBoundingClientRect().left) / pps)) : null;
        onGhost(span);
      },
      onEnd: (moved, ev, cancelled) => {
        onGhost(null);
        if (!moved) addAdjustment(spanAt(clips, clock.time));
        else if (!cancelled && span) addAdjustment(span);
      },
    });
  };
  // A keyboard press arrives as a click without a pointer (detail 0); pointer presses are handled above.
  const key = (e) => {
    if (e.detail === 0) addAdjustment(spanAt(clips, clock.time));
  };
  return html`<button type="button" class="tl-adjust-add" onPointerDown=${down} onClick=${key}
    title="加入調整圖層：點一下蓋住播放頭所在的鏡頭，或按住拖到時間軸上的鏡頭再放開"
    aria-label="加入調整圖層"><${Icon} name="plus" size=${12} /></button>`;
}

export function AdjustTrack({ project, ctx, editSel }) {
  const [drag, setDrag] = useState(null);
  const { pps } = ctx;

  const begin = (e, item, mode) => {
    const snap = ctx.snapper({ adjust: item.id });
    const base = { start: Number(item.start || 0), length: Number(item.length || MIN_ITEM) };
    const end = base.start + base.length;
    let next = null;
    dragPointer(e, {
      threshold: mode === "move" ? 3 : 1,
      onMove: (dx, ev) => {
        const shift = dx / pps;
        if (mode === "move") {
          next = { start: round(Math.max(0, snapRange(base.start + shift, base.length, snap, ev)), 3), length: base.length };
        } else if (mode === "l") {
          const start = round(clamp(snap(base.start + shift, ev), 0, end - MIN_ITEM), 3);
          next = { start, length: round(end - start, 3) };
        } else {
          next = { start: base.start, length: round(clamp(snap(end + shift, ev) - base.start, MIN_ITEM, MAX_TIME), 3) };
        }
        const text = mode === "move" ? `開始 ${next.start.toFixed(2)} 秒` : `長度 ${next.length.toFixed(2)} 秒`;
        setDrag({ id: item.id, mode, ...next, tip: { t: mode === "r" ? next.start + next.length : next.start, text } });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (!moved) {
          setState({ editSel: { type: "adjust", id: item.id } });
          return;
        }
        if (cancelled || !next) return;
        const patch = {};
        for (const k of ["start", "length"]) if (next[k] !== base[k]) patch[k] = next[k];
        if (mode === "l" && patch.start != null) Object.assign(patch, keyPatch(item, shiftKeys(item, patch.start - base.start)));
        if (Object.keys(patch).length) {
          ops([{ op: "adjust.update", id: item.id, patch }], { label: mode === "move" ? "移動調整圖層" : "修改調整圖層長度" });
        }
      },
    });
  };

  const items = [...(project.adjustments || [])].sort((a, b) => Number(a.start || 0) - Number(b.start || 0));
  return html`${items.map((item) => {
    const base = { start: Number(item.start || 0), length: Number(item.length || MIN_ITEM) };
    const mine = drag && drag.id === item.id ? drag : null;
    const cur = mine || base;
    const sel = !!editSel && editSel.type === "adjust" && editSel.id === item.id;
    const plain = isNeutral(item.grade);
    const cls = ["tl-adj", sel && "is-selected", item.hidden && "is-off", drag && drag.id === item.id && "is-dragging"]
      .filter(Boolean).join(" ");
    return html`<div key=${item.id} class=${cls} style=${`left:${cur.start * pps}px;width:${Math.max(4, cur.length * pps)}px`}
      data-sel=${`adjust:${item.id}`}
      title=${`${adjustName(item)}${item.hidden ? "（已停用）" : ""}\n${fmtSeconds(cur.start, 2)} 開始，長 ${fmtSeconds(cur.length, 2)}\n調色套用到下方所有畫面；${
        clipActions(ctx, "拖曳移動，拖曳左右邊緣改長度，雙擊跳到開頭", "雙擊跳到開頭")}`}
      onPointerDown=${(e) => begin(e, item, "move")} onDblClick=${() => setTime(cur.start)}>
      <${Icon} name=${item.hidden ? "eye-off" : "palette"} size=${12} class="tl-adj-icon" />
      <span class="tl-adj-name">${adjustName(item)}</span>
      ${plain && cur.length * pps > 110 && html`<span class="tl-adj-note">未調色</span>`}
      <${KeyMarks} item=${item} pps=${pps} drag=${mine} base=${base} length=${cur.length} />
      <${Handles} onDown=${(e, side) => begin(e, item, side)} />
    </div>`;
  })}
  <${Tip} tip=${drag && drag.tip} pps=${pps} />`;
}
