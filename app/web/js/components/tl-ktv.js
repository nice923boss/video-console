// The KTV wipe marks of a lyric caption on the edit timeline (vc/ktv.py, lib/karaoke.js): a tick where each
// character starts turning the sung colour and one where the last is done. On the selected caption each character
// shows between its ticks and a tick can be dragged to fit the singing: alone it stays between its neighbours,
// with Shift the ticks after it move along. Ticks on top of each other (a character given no time) part by
// direction: a drag to the left takes the first of them, to the right the last. Snapping (the playhead among
// others) works as on the other drags, Alt skips it. One drag is one undo step; caption.update with kt keeps
// exactly what was dragged.
import { html, useState } from "../lib/h.js";
import { ops } from "../lib/actions.js";
import { ktvMarks, dragMarks } from "../lib/karaoke.js";
import { resolveStyle } from "../lib/caption-style.js";
import { dragPointer } from "./tl-tracks.js";

// The caption's marks when its style wipes it, else null (a caption shown plainly has none to drag).
export function wipeMarks(project, cap) {
  return resolveStyle(project, cap).highlight ? ktvMarks(cap) : null;
}

const tickText = (chars, i, at) => (i < chars.length
  ? `「${chars[i]}」${i ? "" : "（第一個字）"}開始變色 ${at.toFixed(2)} 秒` : `最後一個字唱完 ${at.toFixed(2)} 秒`);

// kt: the caption's marks; offset: seconds its start moved in a left trim being dragged (the marks keep their place
// on the timeline, as ktv.follow will); active: the caption is selected in the select tool; snapper as ctx.snapper;
// onTip(tip or null) shows the drag's tooltip on the lane.
export function KtvMarks({ cap, kt, pps, offset = 0, active, snapper, onTip }) {
  const [live, setLive] = useState(null);
  const shown = live || kt;
  const chars = [...(cap.text || "").trim()];
  const x = (t) => `${((t - offset) * pps).toFixed(1)}px`;

  const begin = (e, pressed) => {
    const len = cap.end - cap.start;
    const snap = snapper({ cap: cap.id });
    const pile = kt.map((_, j) => j).filter((j) => Math.abs(kt[j] - kt[pressed]) * pps < 2);
    let next = null;
    dragPointer(e, {
      threshold: 1,
      onMove: (dx, ev) => {
        const i = dx < 0 ? pile[0] : pile[pile.length - 1];
        const want = snap(cap.start + kt[i] + dx / pps, ev) - cap.start;
        next = dragMarks(kt, i, want, len, ev.shiftKey);
        const tip = tickText(chars, i, cap.start + next[i]) + (ev.shiftKey ? "，後面的字一起移動" : "（按住 Shift 後面的字一起移動）");
        setLive(next);
        onTip({ t: cap.start + next[i], text: tip });
      },
      onEnd: (moved, ev, cancelled) => {
        setLive(null);
        onTip(null);
        if (!moved) return;
        const stop = (c) => c.stopPropagation(); // the click after the drag must not reach the caption
        window.addEventListener("click", stop, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", stop, { capture: true }), 0);
        if (cancelled || !next || next.every((t, j) => t === kt[j])) return;
        ops([{ op: "caption.update", id: cap.id, patch: { kt: next } }], { label: "調整刷字關鍵幀" });
      },
    });
  };

  return html`<div class=${`tl-kt${active ? " is-active" : ""}`}>
      ${active && chars.map((ch, i) => /\s/.test(ch) ? null : html`<span key=${`c${i}`} class=${`tl-kt-ch${i % 2 ? " is-odd" : ""}`}
        style=${`left:${x(shown[i])};width:${((shown[i + 1] - shown[i]) * pps).toFixed(1)}px`}>${ch}</span>`)}
      ${shown.map((t, i) => html`<i key=${i} class="tl-kt-mark" style=${`left:${x(t)}`}
        title=${active ? `${tickText(chars, i, cap.start + t)}\n左右拖曳調整，按住 Shift 後面的字一起移動，按住 Alt 不吸附` : null}
        onPointerDown=${active ? (e) => begin(e, i) : null}></i>`)}
    </div>`;
}
