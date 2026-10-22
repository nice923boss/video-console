// Lane contents of the edit timeline: picture clips (trim and reorder) and their own sound, captions,
// soundtrack items and picture-in-picture clips, plus the pointer-drag and snapping helpers they share. A drag previews in local state and writes a
// single op on release, so every drag is one undo step.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { ops, updateShot, moveShot, focusShot } from "../lib/actions.js";
import { setTime } from "../lib/clock.js";
import { fileUrl, fmtSeconds, shotLabel, newId, round, clamp } from "../lib/format.js";
import { buildSequence, audioLength } from "../lib/sequence.js";
import { VOICE_STATES, voiceState, castMember } from "../lib/voice.js";
import { takeFromMedia } from "../views/library.js";
import { Icon } from "./icons.js";
import { openMenu } from "./overlay.js";
import { SHOT_COLORS } from "./insp-content.js";

// Row heights in CSS pixels; timeline.js uses the same numbers for the label column.
export const VIDEO_H = 60;
export const CAPTION_H = 30;
export const AUDIO_H = 44;
export const PIP_H = 44;
export const ADD_H = 28;

const SNAP_PX = 8;
const MIN_VIDEO = 0.1;
const MIN_STILL = 0.2;
const MAX_STILL = 600;
const MIN_ITEM = 0.1;
const MAX_TIME = 36000;
const TILE_H = VIDEO_H - 11; // the row's 1px bottom border, then .tl-clip sits 5px in from top and bottom
const STRIP_FRAMES = 8;
const WAVE_MAX_PX = 8000;
const SOURCE_ICON = { video: "video", image: "image", empty: "film" };

// Right-click on a picture clip: tag it with a colour, e.g. to mark a long pause for review before cutting it.
function clipMenu(e, shot, label) {
  setState({ editSel: { type: "shot", id: shot.id } });
  focusShot(shot.id, { tab: "edit" });
  const current = shot.color ?? null;
  openMenu(e, [
    { header: `${label}：標籤顏色` },
    ...SHOT_COLORS.map((c) => ({ label: c.label, swatch: c.value, checked: current === c.value,
      onClick: () => current !== c.value && updateShot(shot.id, { color: c.value }, { label: "修改標籤顏色" }) })),
  ]);
}

// Left-button drag tracked on window. onMove(dx, ev) starts once the pointer passed `threshold` px
// sideways (or in any direction with axis "xy"); onEnd(moved, ev, cancelled) always runs. Escape
// cancels the drag.
export function dragPointer(e, { onMove, onEnd, threshold = 3, axis = "x" }) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  const x0 = e.clientX;
  const y0 = e.clientY;
  let moved = false;
  const move = (ev) => {
    const dx = ev.clientX - x0;
    const far = axis === "xy" ? Math.max(Math.abs(dx), Math.abs(ev.clientY - y0)) : Math.abs(dx);
    if (!moved && far < threshold) return;
    moved = true;
    if (onMove) onMove(dx, ev);
  };
  const finish = (ev, cancelled) => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", cancel);
    window.removeEventListener("keydown", key, true);
    if (onEnd) onEnd(moved, ev, cancelled);
  };
  const up = (ev) => finish(ev, false);
  const cancel = (ev) => finish(ev, true);
  const key = (ev) => {
    if (ev.key !== "Escape") return;
    ev.preventDefault();
    ev.stopPropagation();
    finish(ev, true);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", cancel);
  window.addEventListener("keydown", key, true);
}

// Pull a time to the nearest point within SNAP_PX; holding Alt skips snapping for that move.
export function makeSnap(points, pps) {
  const limit = SNAP_PX / pps;
  return (t, ev) => {
    if (ev && ev.altKey) return t;
    let best = t;
    let dist = limit;
    for (const p of points) {
      const d = Math.abs(p - t);
      if (d < dist) {
        dist = d;
        best = p;
      }
    }
    return best;
  };
}

// Snap whichever edge of [start, start + len] lands closer to a point.
function snapRange(start, len, snap, ev) {
  const a = snap(start, ev) - start;
  const b = snap(start + len, ev) - (start + len);
  if (!a) return start + b;
  if (!b) return start + a;
  return start + (Math.abs(a) <= Math.abs(b) ? a : b);
}

const laneTime = (e, pps) => {
  const lane = e.currentTarget.closest(".tl-lane");
  return lane ? Math.max(0, (e.clientX - lane.getBoundingClientRect().left) / pps) : 0;
};

function Tip({ tip, pps }) {
  if (!tip) return null;
  return html`<div class="tl-drag-tip mono" style=${`left:${tip.t * pps}px`}>${tip.text}</div>`;
}

function Handles({ onDown }) {
  return html`<i class="tl-handle is-left" onPointerDown=${(e) => onDown(e, "l")}></i>
    <i class="tl-handle is-right" onPointerDown=${(e) => onDown(e, "r")}></i>`;
}

// ---------------------------------------------------------------- picture lane
const patchShot = (project, id, patch) => ({
  ...project,
  shots: project.shots.map((s) => (s.id === id ? { ...s, ...patch } : s)),
});

function moveTarget(project, id, rest, k) {
  const others = project.shots.filter((s) => s.id !== id);
  if (k < rest.length) return others.findIndex((s) => s.id === rest[k].shot.id);
  return others.findIndex((s) => s.id === rest[rest.length - 1].shot.id) + 1;
}

function commitTrim(clip, patch) {
  const id = clip.shot.id;
  if ("trimIn" in patch) {
    const v = round(patch.trimIn, 3);
    if (Math.abs(v - clip.in) > 1e-3) updateShot(id, { trimIn: v }, { label: "修改入點" });
  } else if ("trimOut" in patch) {
    const v = round(patch.trimOut, 3);
    const end = Number(clip.take.duration);
    if (Math.abs(v - clip.out) > 1e-3) updateShot(id, { trimOut: v >= end - 1e-3 ? null : v }, { label: "修改出點" });
  } else {
    const v = round(patch.duration, 2);
    if (Math.abs(v - clip.length) > 1e-3) updateShot(id, { duration: v }, { label: "修改片長" });
  }
}

function selectShot(id, additive) {
  setState({ editSel: { type: "shot", id } });
  focusShot(id, { additive });
}

// Tiles always keep the picture's own aspect, however long the clip. Consecutive tiles that show the same
// strip frame become one rect filled with a repeating pattern, so a long clip costs at most STRIP_FRAMES rects.
function Tiles({ clip, geo, pid, pps, ar }) {
  const take = clip.take;
  if (!take) return html`<div class="tl-tiles"></div>`;
  if (clip.source === "video" && take.strip) {
    const url = fileUrl(pid, take.strip);
    const dur = Number(take.duration) || 1;
    const tileW = TILE_H * (take.width && take.height ? take.width / take.height : ar);
    const count = Math.max(1, Math.ceil((geo.length * pps) / tileW));
    const runs = [];
    for (let k = 0; k < count; k += 1) {
      const t = geo.in + ((k + 0.5) * tileW) / pps;
      const frame = clamp(Math.floor((t / dur) * STRIP_FRAMES), 0, STRIP_FRAMES - 1);
      const last = runs[runs.length - 1];
      if (last && last.frame === frame) last.n += 1;
      else runs.push({ frame, k, n: 1 });
    }
    const pat = (frame) => `tlp-${clip.shot.id}-${frame}`;
    return html`<svg class="tl-tiles is-strip">
      <defs>${runs.map((r) => html`<pattern key=${r.frame} id=${pat(r.frame)} patternUnits="userSpaceOnUse" width=${tileW} height=${TILE_H}>
        <image href=${url} x=${-r.frame * tileW} width=${tileW * STRIP_FRAMES} height=${TILE_H} preserveAspectRatio="none" />
      </pattern>`)}</defs>
      ${runs.map((r) => html`<rect key=${r.frame} x=${r.k * tileW} width=${r.n * tileW} height=${TILE_H} fill=${`url(#${pat(r.frame)})`} />`)}
    </svg>`;
  }
  const path = take.thumb || (clip.source === "image" ? take.file : "");
  if (!path) return html`<div class="tl-tiles"></div>`;
  return html`<div class="tl-tiles" style=${`background-image:url("${fileUrl(pid, path)}");background-size:auto 100%`}></div>`;
}

export function VideoTrack({ project, clips, ctx, editSel, focus }) {
  const transitions = useStore((s) => s.presets.transitions || []);
  const [drag, setDrag] = useState(null);
  const shown = drag && drag.clips ? drag.clips : clips;
  const { pps } = ctx;

  const beginMove = (e, clip) => {
    const id = clip.shot.id;
    const ci = clips.findIndex((c) => c.shot.id === id);
    const rest = clips.filter((c) => c.shot.id !== id);
    const additive = e.shiftKey || e.ctrlKey || e.metaKey;
    let k = ci;
    dragPointer(e, {
      threshold: 4,
      onMove: (dx) => {
        const center = clip.start + clip.length / 2 + dx / pps;
        k = rest.filter((c) => c.start + c.length / 2 < center).length;
        const at = !rest.length || k === ci ? null : k < rest.length ? rest[k].start : rest[k - 1].start + rest[k - 1].length;
        setDrag({ id, dx, insert: at });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (!moved) selectShot(id, additive);
        else if (!cancelled && rest.length && k !== ci) moveShot(id, moveTarget(project, id, rest, k));
      },
    });
  };

  const beginTrim = (e, clip, side) => {
    const id = clip.shot.id;
    const snap = ctx.snapper({ shot: id });
    const video = clip.source === "video";
    const end = video ? Number(clip.take.duration) : 0;
    let patch = null;
    dragPointer(e, {
      threshold: 1,
      onMove: (dx, ev) => {
        const shift = dx / pps;
        if (side === "r") {
          const len = snap(clip.start + clip.length + shift, ev) - clip.start;
          let text;
          if (video) {
            const out = clamp(clip.in + len, clip.in + MIN_VIDEO, end);
            patch = { trimOut: out };
            text = `出點 ${out.toFixed(2)} 秒`;
          } else {
            const d = clamp(len, MIN_STILL, MAX_STILL);
            patch = { duration: d };
            text = `片長 ${d.toFixed(2)} 秒`;
          }
          const next = buildSequence(patchShot(project, id, patch));
          const c = next.find((x) => x.shot.id === id);
          setDrag({ id, clips: next, tip: { t: c.start + c.length, text } });
        } else {
          let s = snap(clip.start + shift, ev) - clip.start;
          let text;
          if (video) {
            const tIn = clamp(clip.in + s, 0, clip.out - MIN_VIDEO);
            s = tIn - clip.in;
            patch = { trimIn: tIn };
            text = `入點 ${tIn.toFixed(2)} 秒`;
          } else {
            const d = clamp(clip.length - s, MIN_STILL, MAX_STILL);
            s = clip.length - d;
            patch = { duration: d };
            text = `片長 ${d.toFixed(2)} 秒`;
          }
          const geo = { start: clip.start + s, length: clip.length - s, in: clip.in + s };
          setDrag({ id, geo, tip: { t: geo.start, text } });
        }
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (moved && !cancelled && patch) commitTrim(clip, patch);
      },
    });
  };

  const transLabel = (type) => (transitions.find((x) => x.id === type) || { label: type }).label;
  const marks = [];
  shown.forEach((clip, i) => {
    const next = shown[i + 1];
    if (!next || !(clip.transition.duration > 0)) return;
    marks.push(html`<button type="button" key=${`t-${clip.shot.id}`} class="tl-trans"
      style=${`left:${next.start * pps}px;width:${Math.max(6, clip.transition.duration * pps)}px`}
      title=${`${transLabel(clip.transition.type)}，${fmtSeconds(clip.transition.duration, 2)}（點一下設定轉場）`}
      onPointerDown=${(e) => e.stopPropagation()}
      onClick=${() => {
        setState({ editSel: { type: "shot", id: clip.shot.id } });
        focusShot(clip.shot.id, { tab: "edit" });
      }}></button>`);
  });

  const shotSel = !editSel || editSel.type === "shot";
  return html`${shown.map((clip) => {
    const id = clip.shot.id;
    const geo = drag && drag.geo && drag.id === id ? drag.geo : { start: clip.start, length: clip.length, in: clip.in };
    const moving = drag && drag.dx != null && drag.id === id;
    const cls = ["tl-clip", `is-${clip.source}`, shotSel && focus === id && "is-selected", moving && "is-dragging",
      clip.shot.color && "has-color"].filter(Boolean).join(" ");
    const label = shotLabel(clip.shot, clip.index);
    const style = `left:${geo.start * pps}px;width:${Math.max(2, geo.length * pps)}px`
      + (clip.shot.color ? `;--shot-color:${clip.shot.color}` : "")
      + (moving ? `;transform:translateX(${drag.dx}px)` : "");
    return html`<div key=${id} class=${cls} style=${style}
      title=${`${label}，${fmtSeconds(clip.length, 2)}${clip.shot.notes ? `\n${clip.shot.notes}` : ""}\n拖曳左右邊緣修剪，拖曳中間調整順序，雙擊跳到該處，右鍵標記顏色`}
      onPointerDown=${(e) => beginMove(e, clip)} onDblClick=${(e) => setTime(laneTime(e, pps))}
      onContextMenu=${(e) => clipMenu(e, clip.shot, label)}>
      <${Tiles} clip=${clip} geo=${geo} pid=${project.id} pps=${pps} ar=${ctx.ar} />
      <div class="tl-clip-label"><${Icon} name=${SOURCE_ICON[clip.source]} size=${12} /><span>${label}</span></div>
      ${clip.source === "empty" && geo.length * pps > 90 && html`<span class="tl-clip-empty">尚未產生畫面</span>`}
      <span class="tl-clip-dur mono">${fmtSeconds(geo.length, 1)}</span>
      <${Handles} onDown=${(e, side) => beginTrim(e, clip, side)} />
    </div>`;
  })}
  ${marks}
  ${drag && drag.insert != null && html`<i class="tl-insert" style=${`left:${drag.insert * pps}px`}></i>`}
  <${Tip} tip=${drag && drag.tip} pps=${pps} />`;
}

// ---------------------------------------------------------------- caption lane
export function CaptionTrack({ project, ctx, editSel }) {
  const [drag, setDrag] = useState(null);
  const { pps } = ctx;

  const begin = (e, cap, mode) => {
    const snap = ctx.snapper({ cap: cap.id });
    const len = cap.end - cap.start;
    let next = null;
    dragPointer(e, {
      threshold: mode === "move" ? 3 : 1,
      onMove: (dx, ev) => {
        const shift = dx / pps;
        let { start, end } = cap;
        if (mode === "move") {
          start = Math.max(0, snapRange(cap.start + shift, len, snap, ev));
          end = start + len;
        } else if (mode === "l") {
          start = clamp(snap(cap.start + shift, ev), 0, cap.end - MIN_ITEM);
        } else {
          end = clamp(snap(cap.end + shift, ev), cap.start + MIN_ITEM, MAX_TIME);
        }
        next = { start: round(start, 3), end: round(end, 3) };
        const text = mode === "move" ? `${next.start.toFixed(2)} 到 ${next.end.toFixed(2)} 秒` : `長度 ${(next.end - next.start).toFixed(2)} 秒`;
        setDrag({ id: cap.id, ...next, tip: { t: mode === "r" ? next.end : next.start, text } });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (!moved) {
          setState({ editSel: { type: "caption", id: cap.id } });
          return;
        }
        if (cancelled || !next || (next.start === cap.start && next.end === cap.end)) return;
        ops([{ op: "caption.update", id: cap.id, patch: next }], { label: mode === "move" ? "移動字幕" : "調整字幕長度" });
      },
    });
  };

  return html`${(project.captions || []).map((cap) => {
    const cur = drag && drag.id === cap.id ? drag : cap;
    const sel = editSel && editSel.type === "caption" && editSel.id === cap.id;
    const text = (cap.text || "").trim() || "（空白字幕）";
    const member = castMember(project, cap.speaker);
    const voice = member ? voiceState(project, cap) : null;
    const cls = ["tl-cap", sel && "is-selected", drag && drag.id === cap.id && "is-dragging", voice === "stale" && "is-stale"]
      .filter(Boolean).join(" ");
    const who = member ? `${member.name || "未命名"}，${VOICE_STATES[voice].label}\n` : "";
    const bound = cap.shot ? project.shots.findIndex((s) => s.id === cap.shot) : -1;
    const link = bound >= 0 ? `\n綁定 ${shotLabel(project.shots[bound], bound)}` : "";
    return html`<div key=${cap.id} class=${cls}
      style=${`left:${cur.start * pps}px;width:${Math.max(4, (cur.end - cur.start) * pps)}px`}
      title=${`${who}${text}\n${fmtSeconds(cap.start, 2)} 到 ${fmtSeconds(cap.end, 2)}，雙擊跳到開頭${link}`}
      onPointerDown=${(e) => begin(e, cap, "move")} onDblClick=${() => setTime(cap.start)}>
      ${bound >= 0 && html`<${Icon} name="link" size=${11} class="tl-cap-link" />`}
      ${member && html`<b class="tl-cap-who">${member.name || "未命名"}</b>`}
      <span class="tl-cap-text">${text}</span>
      <${Handles} onDown=${(e, side) => begin(e, cap, side)} />
    </div>`;
  })}
  <${Tip} tip=${drag && drag.tip} pps=${pps} />`;
}

// ---------------------------------------------------------------- soundtrack lane
const waveCache = new Map();

function loadWave(url) {
  if (!waveCache.has(url)) {
    const forget = () => {
      waveCache.delete(url);
      return null;
    };
    waveCache.set(url, fetch(url).then((r) => (r.ok ? r.json() : forget())).catch(forget));
  }
  return waveCache.get(url);
}

function drawWave(canvas, data, tIn, len, width) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.min(WAVE_MAX_PX, Math.round(width * dpr)));
  const h = Math.max(1, Math.round((canvas.clientHeight || AUDIO_H - 16) * dpr));
  canvas.width = w;
  canvas.height = h;
  const g = canvas.getContext("2d");
  const peaks = data && Array.isArray(data.peaks) ? data.peaks : null;
  const dur = data ? Number(data.duration) : 0;
  if (!g || !peaks || !peaks.length || !(dur > 0)) return;
  g.fillStyle = getComputedStyle(canvas).color;
  const n = peaks.length;
  for (let x = 0; x < w; x += 1) {
    const i0 = Math.max(0, Math.floor(((tIn + (x / w) * len) / dur) * n));
    const i1 = Math.min(n, Math.max(i0 + 1, Math.ceil(((tIn + ((x + 1) / w) * len) / dur) * n)));
    let peak = 0;
    for (let i = i0; i < i1; i += 1) if (peaks[i] > peak) peak = peaks[i];
    const bar = Math.max(1, (peak / 100) * h * 0.9);
    g.fillRect(x, (h - bar) / 2, 1, bar);
  }
}

function Wave({ pid, media, tIn, len, width }) {
  const ref = useRef(null);
  const [data, setData] = useState(null);
  const url = media && media.wave ? fileUrl(pid, media.wave) : "";
  useEffect(() => {
    let alive = true;
    setData(null);
    if (url) loadWave(url).then((d) => alive && setData(d));
    return () => {
      alive = false;
    };
  }, [url]);
  useEffect(() => {
    if (ref.current) drawWave(ref.current, data, tIn, len, width);
  }, [data, tIn, len, width]);
  return url ? html`<canvas class="tl-wave" ref=${ref}></canvas>` : null;
}

export function AudioClip({ project, item, ctx, selected }) {
  const [drag, setDrag] = useState(null);
  const { pps } = ctx;
  const media = (project.media || {})[item.mediaId];
  const full = media && media.duration ? Number(media.duration) : 0;
  const base = { start: Number(item.start || 0), in: Number(item.in || 0), out: item.out == null ? null : Number(item.out) };
  const cur = drag || base;
  const len = audioLength(cur, media);

  const begin = (e, mode) => {
    const snap = ctx.snapper({ audio: item.id });
    const a0 = base.in;
    const b0 = base.out == null ? a0 + audioLength(base, media) : base.out;
    const cap = full > 0 ? full : MAX_TIME;
    let next = null;
    let patch = null;
    dragPointer(e, {
      threshold: mode === "move" ? 3 : 1,
      onMove: (dx, ev) => {
        const shift = dx / pps;
        let text;
        if (mode === "move") {
          const start = round(Math.max(0, snapRange(base.start + shift, b0 - a0, snap, ev)), 3);
          next = { ...base, start };
          patch = { start };
          text = `開始 ${start.toFixed(2)} 秒`;
        } else if (mode === "l") {
          const s = snap(base.start + shift, ev) - base.start;
          const tIn = round(clamp(a0 + s, Math.max(0, a0 - base.start), b0 - MIN_ITEM), 3);
          const start = round(base.start + (tIn - a0), 3);
          next = { ...base, start, in: tIn };
          patch = { start, in: tIn };
          text = `入點 ${tIn.toFixed(2)} 秒`;
        } else {
          const tOut = round(clamp(snap(base.start + (b0 - a0) + shift, ev) - base.start + a0, a0 + MIN_ITEM, cap), 3);
          next = { ...base, out: tOut };
          patch = { out: full > 0 && tOut >= full - 1e-3 ? null : tOut };
          text = `出點 ${tOut.toFixed(2)} 秒`;
        }
        const t = mode === "r" ? next.start + audioLength(next, media) : next.start;
        setDrag({ ...next, tip: { t, text } });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (!moved) {
          setState({ editSel: { type: "audio", id: item.id } });
          return;
        }
        if (cancelled || !patch) return;
        const label = mode === "move" ? "移動音軌" : mode === "l" ? "修改音軌入點" : "修改音軌出點";
        ops([{ op: "audio.update", id: item.id, patch }], { label });
      },
    });
  };

  const width = Math.max(4, len * pps);
  const name = item.name || (media && media.name) || "音訊";
  const cls = ["tl-audio", selected && "is-selected", item.muted && "is-muted", !media && "is-missing", drag && "is-dragging"]
    .filter(Boolean).join(" ");
  const fi = Math.min(Number(item.fadeIn || 0), len / 2) * pps;
  const fo = Math.min(Number(item.fadeOut || 0), len / 2) * pps;
  return html`<div class=${cls} style=${`left:${cur.start * pps}px;width:${width}px`}
    title=${`${name}\n${fmtSeconds(cur.start, 2)} 開始，長 ${fmtSeconds(len, 2)}，雙擊跳到開頭`}
    onPointerDown=${(e) => begin(e, "move")} onDblClick=${() => setTime(cur.start)}>
    ${media && html`<${Wave} pid=${project.id} media=${media} tIn=${cur.in} len=${len} width=${width} />`}
    ${fi > 1 && html`<i class="tl-fade is-in" style=${`width:${fi}px`}></i>`}
    ${fo > 1 && html`<i class="tl-fade is-out" style=${`width:${fo}px`}></i>`}
    <div class="tl-audio-label"><${Icon} name=${item.muted ? "volume-x" : "music"} size=${12} />
      <span>${media ? name : "找不到音訊檔"}</span></div>
    <${Handles} onDown=${begin} />
    <${Tip} tip=${drag && drag.tip ? { ...drag.tip, t: drag.tip.t - cur.start } : null} pps=${pps} />
  </div>`;
}

// The picture clips' own sound, one block per clip whose video take has sound, with its waveform when the
// take has one (takes imported before waveforms were made for videos have none). It only shows the
// sound: trims and moves happen on the picture lane, and its mute switch silences this lane too.
// A click selects the shot, whose edit tab holds "轉成字幕".
export function hasSourceAudio(clips) {
  return clips.some((c) => c.source === "video" && c.take.hasAudio);
}

export function SourceAudioTrack({ project, clips, ctx, editSel }) {
  const { pps } = ctx;
  return clips.filter((c) => c.source === "video" && c.take.hasAudio).map((c) => {
    const width = Math.max(4, c.length * pps);
    const name = shotLabel(c.shot, c.index);
    const sel = !!editSel && editSel.type === "shot" && editSel.id === c.shot.id;
    return html`<div key=${c.shot.id} class=${`tl-audio tl-source${sel ? " is-selected" : ""}`}
      style=${`left:${c.start * pps}px;width:${width}px`}
      title=${`${name} 的原聲\n${fmtSeconds(c.start, 2)} 開始，長 ${fmtSeconds(c.length, 2)}，雙擊跳到開頭`}
      onPointerDown=${(e) => e.stopPropagation()} onDblClick=${() => setTime(c.start)}
      onClick=${() => {
        setState({ editSel: { type: "shot", id: c.shot.id } });
        focusShot(c.shot.id, { tab: "edit" });
      }}>
      <${Wave} pid=${project.id} media=${c.take} tIn=${c.in} len=${c.length} width=${width} />
      <div class="tl-audio-label"><${Icon} name="volume" size=${12} /><span>${name}</span></div>
    </div>`;
  });
}

// ---------------------------------------------------------------- picture-in-picture lanes
// The lane under the pointer while an overlay is dragged up or down: another track, the new-track row
// or the picture lane.
function pipLaneAt(ev) {
  const lane = document.elementsFromPoint(ev.clientX, ev.clientY).find((el) => el.classList.contains("tl-lane"));
  const target = lane ? lane.parentElement.dataset.drop || "" : "";
  return target === "new-layer" || target === "video" || target.startsWith("layer:") ? { lane, target } : null;
}

// An overlay dropped on the picture lane becomes a new shot at `index` with the same piece of the
// file; the shots after it move back by its length.
function overlayToShot(item, media, index) {
  const video = media.kind === "video";
  const len = Number(item.length || MIN_ITEM);
  const tIn = round(Number(item.in || 0), 3);
  const out = round(tIn + len, 3);
  const end = Number(media.duration) || 0;
  const shot = {
    id: newId("s"),
    name: String(item.name || media.name || "").replace(/\.[a-z0-9]{2,5}$/i, "").slice(0, 120),
    duration: round(clamp(len, MIN_STILL, MAX_STILL), video ? 3 : 2),
    ...(video ? { trimIn: tIn, trimOut: end && out >= end - 1e-3 ? null : out } : {}),
  };
  const take = takeFromMedia(media, shot.id);
  ops([
    { op: "shot.add", shot, index },
    { op: "take.add", take },
    { op: "shot.update", id: shot.id, patch: { [video ? "activeVideo" : "activeImage"]: take.id } },
    { op: "overlay.delete", id: item.id },
  ], { label: "移到畫面軌" }).then((res) => {
    if (res) selectShot(shot.id, false);
  });
}

export function OverlayClip({ project, item, ctx, selected }) {
  const [drag, setDrag] = useState(null);
  const { pps } = ctx;
  const media = (project.media || {})[item.mediaId];
  const still = !!media && media.kind === "image";
  const full = !still && media && media.duration ? Number(media.duration) : 0;
  const base = { start: Number(item.start || 0), in: Number(item.in || 0), length: Number(item.length || MIN_ITEM) };
  const cur = drag || base;

  const begin = (e, mode) => {
    const snap = ctx.snapper({ overlay: item.id });
    const home = e.currentTarget.closest(".tl-lane");
    const minLen = still ? MIN_STILL : MIN_ITEM;
    let next = null;
    let target = null;
    let insert = null;
    dragPointer(e, {
      threshold: mode === "move" ? 3 : 1,
      axis: mode === "move" ? "xy" : "x",
      onMove: (dx, ev) => {
        const shift = dx / pps;
        let dy = 0;
        let text;
        insert = null;
        if (mode === "move") {
          const start = round(Math.max(0, snapRange(base.start + shift, base.length, snap, ev)), 3);
          next = { ...base, start };
          text = `開始 ${start.toFixed(2)} 秒`;
          const hit = pipLaneAt(ev);
          target = hit && hit.lane !== home && (hit.target !== "video" || media) ? hit.target : null;
          if (target) {
            dy = hit.lane.getBoundingClientRect().top - home.getBoundingClientRect().top;
            if (target === "video") {
              insert = ctx.insertAt(start);
              text = "穿插到這裡，後面的鏡頭往後退";
            } else text += target === "new-layer" ? "，放到新的畫面軌" : "，換到這條軌道";
          }
        } else if (mode === "l") {
          const end = base.start + base.length;
          const lo = Math.max(0, still ? end - MAX_STILL : base.start - base.in);
          const start = round(clamp(snap(base.start + shift, ev), lo, end - minLen), 3);
          const tIn = still ? base.in : round(base.in + start - base.start, 3);
          next = { start, in: tIn, length: round(end - start, 3) };
          text = still ? `長度 ${next.length.toFixed(2)} 秒` : `入點 ${tIn.toFixed(2)} 秒`;
        } else {
          const hi = still ? MAX_STILL : full > 0 ? full - base.in : MAX_TIME;
          const length = round(clamp(snap(base.start + base.length + shift, ev) - base.start, minLen, hi), 3);
          next = { ...base, length };
          text = `長度 ${length.toFixed(2)} 秒`;
        }
        const t = insert ? insert.at : mode === "r" ? next.start + next.length : next.start;
        setDrag({ ...next, dy, insert, tip: { t, text } });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (!moved) {
          setState({ editSel: { type: "overlay", id: item.id } });
          return;
        }
        if (cancelled || !next) return;
        if (insert) {
          overlayToShot(item, media, insert.index);
          return;
        }
        const patch = {};
        for (const k of ["start", "in", "length"]) if (next[k] !== base[k]) patch[k] = next[k];
        const list = [];
        let label = mode === "move" ? "移動片段" : "修剪片段";
        if (target === "new-layer") {
          patch.layer = newId("l");
          list.push({ op: "layer.add", layer: { id: patch.layer } });
          label = "移到新的畫面軌";
        } else if (target) {
          patch.layer = target.slice(6);
          label = "移到其他畫面軌";
        }
        if (Object.keys(patch).length) ops([...list, { op: "overlay.update", id: item.id, patch }], { label });
      },
    });
  };

  const width = Math.max(4, cur.length * pps);
  const name = item.name || (media && media.name) || "片段";
  const thumb = media ? media.thumb || (still ? media.file : "") : "";
  const cls = ["tl-clip", "tl-pip", still ? "is-image" : "is-video", selected && "is-selected", !media && "is-missing",
    drag && "is-dragging"].filter(Boolean).join(" ");
  const style = `left:${cur.start * pps}px;width:${width}px` + (drag && drag.dy ? `;transform:translateY(${drag.dy}px)` : "");
  return html`<div class=${cls} style=${style}
    title=${`${name}\n${fmtSeconds(cur.start, 2)} 開始，長 ${fmtSeconds(cur.length, 2)}\n拖曳移動，上下拖曳換軌道，拖進「畫面」軌可穿插成鏡頭，拖曳左右邊緣修剪，雙擊跳到開頭`}
    onPointerDown=${(e) => begin(e, "move")} onDblClick=${() => setTime(cur.start)}>
    ${thumb && html`<div class="tl-tiles" style=${`background-image:url("${fileUrl(project.id, thumb)}");background-size:auto 100%`}></div>`}
    <div class="tl-clip-label"><${Icon} name=${still ? "image" : "video"} size=${12} /><span>${media ? name : "找不到素材檔"}</span></div>
    <span class="tl-clip-dur mono">${fmtSeconds(cur.length, 1)}</span>
    <${Handles} onDown=${begin} />
  </div>
  ${drag && drag.insert && html`<i class="tl-insert"
    style=${`left:${drag.insert.at * pps}px;top:${drag.dy}px;bottom:auto;height:${VIDEO_H}px`}></i>`}
  <${Tip} tip=${drag && drag.tip} pps=${pps} />`;
}
