// The volume line on an audio block: volume against time over the waveform (silent at the bottom, 100% at mid
// height, 400% at the top), with a dot on each key. On a selected block, dragging the line up or down moves the
// two keys either side of the pointer by the same amount (before the first key or after the last: that key;
// no keys: the whole clip's volume), so the stretch between two keys can be lowered or pulled down to mute it.
import { html, useState } from "../lib/h.js";
import { ops, updateShot } from "../lib/actions.js";
import { round, clamp } from "../lib/format.js";
import { valueAt } from "../lib/keyframes.js";
import { dragPointer, Tip } from "./tl-tracks.js";

const up = (v) => Math.sqrt(clamp(v, 0, 4) / 4); // 0..1 up the block
const level = (f) => 4 * f * f;
const DRAG_SPAN = 100; // px of drag for the block's full height at least, so a short block still adjusts finely

// The keys a drag at t (seconds into the clip) moves.
function segment(keys, t) {
  if (!keys.length) return [];
  const i = keys.findIndex((k) => k.t > t);
  if (i === 0) return [0];
  if (i === -1) return [keys.length - 1];
  return [i - 1, i];
}

// keys: [{t, volume}] seconds into the clip (they may lie past its ends); volume: the plain volume when there
// are none; max: the most a drag may reach. onCommit({keys, volume}) once, on release (one undo step); keys
// not moved keep their objects.
function VolumeLine({ keys, volume, length, pps, active, max = 4, onCommit }) {
  const [live, setLive] = useState(null);
  const shown = live || { keys, volume };
  const w = Math.max(1, length * pps);
  const item = { keyframes: shown.keys };
  const at = (t) => (shown.keys.length ? Number(valueAt(item, "volume", t, 1)) : shown.volume);
  const times = [0, ...shown.keys.map((k) => k.t).filter((t) => t > 0 && t < length), length];
  const points = times.map((t) => `${(t * pps).toFixed(1)},${(100 * (1 - up(at(t)))).toFixed(2)}`).join(" ");

  const begin = (e) => {
    const box = e.currentTarget.closest("svg").getBoundingClientRect();
    const moving = segment(keys, (e.clientX - box.left) / pps);
    const y0 = e.clientY;
    const x = e.clientX - box.left;
    let next = null;
    dragPointer(e, {
      axis: "xy",
      threshold: 2,
      onMove: (dx, ev) => {
        const shift = (v) => round(level(clamp(up(v) + (y0 - ev.clientY) / Math.max(box.height, DRAG_SPAN), 0, up(max))), 2);
        next = keys.length ? { keys: keys.map((k, i) => (moving.includes(i) ? { ...k, volume: shift(k.volume) } : k)), volume }
          : { keys, volume: shift(volume) };
        setLive({ ...next, x, label: next.keys.length ? next.keys[moving[0]].volume : next.volume });
      },
      onEnd: (moved, ev, cancelled) => {
        setLive(null);
        if (!moved) return;
        // The click that follows the drag must not reach the block (a 原聲 block would reselect its shot).
        const stop = (c) => c.stopPropagation();
        window.addEventListener("click", stop, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", stop, { capture: true }), 0);
        if (!cancelled && next) onCommit(next);
      },
    });
  };

  return html`<svg class=${`tl-vline${active ? " is-active" : ""}`} viewBox=${`0 0 ${w.toFixed(1)} 100`} preserveAspectRatio="none">
      <polyline class="tl-vline-path" points=${points} />
      ${active && html`<polyline class="tl-vline-hit" points=${points} onPointerDown=${begin}>
        <title>上下拖曳調整這兩個關鍵幀之間的音量，拉到最下面是靜音</title></polyline>`}
    </svg>
    ${shown.keys.filter((k) => k.t >= 0 && k.t <= length).map((k) => html`<i key=${k.t} class="tl-vdot"
      style=${`left:${k.t * pps}px;top:calc(13px + (100% - 15px) * ${(1 - up(k.volume)).toFixed(3)})`}></i>`)}
    <${Tip} tip=${live && { t: live.x / pps, text: `${Math.round(live.label * 100)}%` }} pps=${pps} />`;
}

// An audio clip's line: its volume keys, else its volume.
export function AudioVolumeLine({ item, length, pps, active }) {
  const keys = (item.keyframes || []).filter((k) => "volume" in k);
  if (!active && !keys.length) return null;
  const commit = (next) => {
    const moved = new Map(keys.map((k, i) => [k, next.keys[i]]));
    const patch = keys.length ? { keyframes: item.keyframes.map((k) => moved.get(k) || k) } : { volume: next.volume };
    ops([{ op: "audio.update", id: item.id, patch }], { label: "拖曳音量線" });
  };
  return html`<${VolumeLine} keys=${keys} volume=${Number(item.volume ?? 1)} length=${length} pps=${pps} active=${active}
    onCommit=${commit} />`;
}

// A shot's own sound: its volume times the version's hand-set keys (gainKeys, file seconds), else its volume.
export function SourceVolumeLine({ clip, pps, active }) {
  const gain = clip.take.gainKeys || [];
  if (!active && !gain.length) return null;
  const sp = clip.speed || 1;
  const base = Number(clip.shot.volume ?? 1);
  const keys = gain.map((k) => ({ t: (k.t - clip.in) / sp, volume: base * k.volume }));
  const commit = (next) => {
    if (!keys.length) return updateShot(clip.shot.id, { volume: next.volume }, { label: "拖曳音量線" });
    const gainKeys = gain.map((k, i) => (next.keys[i] === keys[i] ? k
      : { t: round(clip.in + next.keys[i].t * sp, 3), volume: round(next.keys[i].volume / base, 4) }));
    return ops([{ op: "take.update", id: clip.take.id, patch: { gainKeys } }], { label: "拖曳音量線" });
  };
  // At 0% shot volume the keys cannot be raised; the 原聲音量 slider brings the whole shot back first.
  return html`<${VolumeLine} keys=${keys} volume=${base} length=${clip.length} pps=${pps}
    active=${active && (base > 0 || !keys.length)} max=${keys.length ? Math.min(4, base * 4) : 4} onCommit=${commit} />`;
}
