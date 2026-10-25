// Trim tool on the picture lane (ED-08, key T). Dragging a clip's edge rolls the cut there: one shot gets longer,
// its neighbour shorter. Dragging its middle slips the footage inside it; Shift+drag slides it between its
// neighbours. None of them changes the total length. The drag previews with lib/roll.js and lands as one
// POST /edit, so the keys and Claude cut the same way.
import { html } from "../lib/h.js";
import { setState } from "../lib/store.js";
import { editCmd, focusShot } from "../lib/actions.js";
import { keyHint } from "../lib/keymap.js";
import { round } from "../lib/format.js";
import { cutRange, slipRange, clampTo, movedCut } from "../lib/roll.js";
import { dragPointer, snapRange } from "./tl-tracks.js";
import { toast } from "./toast.js";
import { IconButton } from "./ui.js";

const TOOLS = [
  ["select", "pointer", "選擇模式", "edit.toolSelect"],
  ["trim", "trim", "修剪模式：拖曳剪接點滾動，拖曳片段滑動內容，按住 Shift 拖曳滑移鏡頭", "edit.toolTrim"],
  ["blade", "blade", "分割模式：點片段就在點的位置切開", "edit.toolBlade"],
];

export function ToolButtons({ tool }) {
  return html`<span class="tl-tools">${TOOLS.map(([id, icon, text, key]) => html`<${IconButton} key=${id} icon=${icon} size=${16}
    active=${tool === id} title=${`${text}${keyHint(key)}`} onClick=${() => setState({ editTool: id })} />`)}</span>`;
}

const sec = (v) => `${v >= 0 ? "+" : ""}${v.toFixed(2)} 秒`;

// What a drag on clips[i] does: the command, which shot it names, how far it may go and how the lane looks
// at d seconds. Null (after saying why) when there is nothing to move.
function plan(clips, i, side, shift) {
  const clip = clips[i];
  if (side) {
    const a = side === "l" ? i - 1 : i;
    if (a < 0 || a + 1 >= clips.length) {
      toast(a < 0 ? "這是第一個鏡頭，前面沒有剪接點可以滾動" : "這是最後一個鏡頭，後面沒有剪接點可以滾動", { level: "warn" });
      return null;
    }
    const [before, after] = [clips[a], clips[a + 1]];
    return { cmd: "roll", id: before.shot.id, range: cutRange(before, after), snapId: before.shot.id,
      view: (d) => ({ clips: movedCut(clips, a, a + 1, d), tip: { t: after.start + d,
        text: `滾動 ${sec(d)}，出點 ${(before.out + d * before.speed).toFixed(2)}，入點 ${(after.in + d * after.speed).toFixed(2)}` } }) };
  }
  if (shift) {
    if (i < 1 || i + 1 >= clips.length) {
      toast("滑移鏡頭要前後都有鏡頭", { level: "warn" });
      return null;
    }
    return { cmd: "slide", id: clip.shot.id, range: cutRange(clips[i - 1], clips[i + 1]), snapId: clips[i - 1].shot.id,
      view: (d) => ({ clips: movedCut(clips, i - 1, i + 1, d), tip: { t: clip.start + d, text: `滑移 ${sec(d)}` } }) };
  }
  if (clip.source !== "video") {
    toast("滑動內容只能用在影片鏡頭", { level: "warn" });
    return null;
  }
  return { cmd: "slip", id: clip.shot.id, range: slipRange(clip), sign: -1,
    view: (d) => ({ id: clip.shot.id, geo: { start: clip.start, length: clip.length, in: clip.in + d * clip.speed },
      tip: { t: clip.start, text: `入點 ${(clip.in + d * clip.speed).toFixed(2)}，出點 ${(clip.out + d * clip.speed).toFixed(2)} 秒` } }) };
}

// Pointer down on a clip (side null) or one of its edges ("l" or "r") while the trim tool is on.
export function trimDown(e, { clips, clip, side, ctx, setDrag }) {
  if (e.button !== 0) return;
  const i = clips.findIndex((c) => c.shot.id === clip.shot.id);
  const p = plan(clips, i, side, !side && e.shiftKey);
  if (!p) {
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  const snap = p.snapId ? ctx.snapper({ shot: p.snapId }) : null;
  const cutAt = p.cmd === "roll" ? clips[side === "l" ? i : i + 1].start : null;
  let by = 0;
  dragPointer(e, {
    threshold: side ? 1 : 3,
    onMove: (dx, ev) => {
      let d = (p.sign || 1) * dx / ctx.pps;
      if (cutAt != null) d = snap(cutAt + d, ev) - cutAt;
      else if (snap) d = snapRange(clip.start + d, clip.length, snap, ev) - clip.start;
      by = clampTo(d, p.range);
      setDrag(p.view(by));
    },
    onEnd: (moved, ev, cancelled) => {
      setDrag(null);
      if (!moved) {
        setState({ editSel: { type: "shot", id: clip.shot.id } });
        focusShot(clip.shot.id);
      }
      if (!moved || cancelled || Math.abs(by) < 1e-3) return;
      editCmd({ cmd: p.cmd, id: p.id, by: round(by, 3) });
    },
  });
}
