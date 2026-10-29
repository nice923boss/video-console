// Lane contents of the edit timeline: picture clips (trim and reorder) and their own sound, captions,
// soundtrack items and picture-in-picture clips, plus the pointer-drag and snapping helpers they share. A drag previews in local state and writes a
// single op on release, so every drag is one undo step.
import { html, useState, useEffect, useRef, useMemo } from "../lib/h.js";
import { useStore, setState, getState } from "../lib/store.js";
import { ops, updateShot, moveShot, focusShot, clickShot } from "../lib/actions.js";
import { setTime } from "../lib/clock.js";
import { releaseFocus } from "../lib/keys.js";
import { fileUrl, fmtSeconds, shotLabel, newId, round, clamp } from "../lib/format.js";
import { buildSequence, audioLength, audioSpeed, audioMedia, audioTrack, VOICE_TRACK } from "../lib/sequence.js";
import { shiftKeys, keyPatch, takeKeys } from "../lib/keyframes.js";
import { VOICE_STATES, voiceState, castMember } from "../lib/voice.js";
import { liftOps, dropOps } from "../lib/lift.js";
import { fitStart, fitGap, edgeRoom, layerSpans } from "../lib/track-fit.js";
import { parseGlossary, glossaryHits, hitText } from "../lib/glossary.js";
import { takeFromMedia } from "../lib/media-actions.js";
import { Icon } from "./icons.js";
import { toast } from "./toast.js";
import { AudioVolumeLine, SourceVolumeLine } from "./tl-volume-line.js";
import { Wave } from "./tl-wave.js";
import { toggleAudioSel, rangeAudioSel } from "./tl-audio-sel.js";
import { trimDown } from "./tl-roll.js";
import { inView } from "./tl-view.js";

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
const TILE_INSET = 11; // the row's 1px bottom border, then .tl-clip sits 5px in from top and bottom
const STRIP_FRAMES = 8;
const SOURCE_ICON = { video: "video", image: "image", empty: "film" };
const BLOCKED = "這條軌道在這裡沒有足夠的空間，放開不會移動";

// A clip's keyframes as diamonds along its bottom edge. While its left edge is being trimmed they keep
// their place on the timeline, as they will once the trim lands.
export function KeyMarks({ item, pps, drag, base, length }) {
  const offset = drag && drag.mode === "l" ? drag.start - base.start : 0;
  const keys = (item.keyframes || []).filter((k) => k.t - offset >= -1e-3 && k.t - offset <= length + 1e-3);
  if (!keys.length) return null;
  return html`<div class="tl-keys">${keys.map((k) => html`<i key=${k.t} class="tl-key" style=${`left:${(k.t - offset) * pps}px`}></i>`)}</div>`;
}

// Left-button drag tracked on window. onMove(dx, ev) starts once the pointer passed `threshold` px
// sideways (or in any direction with axis "xy"); onEnd(moved, ev, cancelled) always runs. Escape
// cancels the drag.
export function dragPointer(e, { onMove, onEnd, threshold = 3, axis = "x" }) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  releaseFocus();
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
export function snapRange(start, len, snap, ev) {
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

// What a press on a clip does with the tool in use, for its tooltip: `select` in the select tool, and
// `dbl` (its double-click) still applies in watch mode.
export function clipActions(ctx, select, dbl) {
  if (ctx.tool === "watch") return `觀看模式：拖曳只移動播放頭，${dbl}`;
  if (ctx.tool === "blade") return "分割模式：點一下就在點的位置切開";
  return select;
}

export function Tip({ tip, pps }) {
  if (!tip) return null;
  return html`<div class=${`tl-drag-tip mono${tip.text === BLOCKED ? " is-blocked" : ""}`} style=${`left:${tip.t * pps}px`}>${tip.text}</div>`;
}

export function Handles({ onDown }) {
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

function liftShot(project, clip, { target, start }) {
  const res = liftOps(project, clip, target, start);
  if (res.error) return toast(res.error, { level: "warn" });
  ops(res.ops, { label: "移到上層軌", link: false }).then((ok) => {
    if (ok) setState({ editSel: { type: "overlay", id: res.id } });
  });
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

// Tiles always keep the picture's own aspect, however long the clip. Consecutive tiles that show the same
// strip frame become one rect filled with a repeating pattern, so a long clip costs at most STRIP_FRAMES rects.
function Tiles({ clip, geo, pid, pps, ar, scale }) {
  const take = clip.take;
  const tileH = Math.round(VIDEO_H * scale) - TILE_INSET;
  if (!take) return html`<div class="tl-tiles"></div>`;
  if (clip.source === "video" && take.strip) {
    const url = fileUrl(pid, take.strip);
    const dur = Number(take.duration) || 1;
    const tileW = tileH * (take.width && take.height ? take.width / take.height : ar);
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
      <defs>${runs.map((r) => html`<pattern key=${r.frame} id=${pat(r.frame)} patternUnits="userSpaceOnUse" width=${tileW} height=${tileH}>
        <image href=${url} x=${-r.frame * tileW} width=${tileW * STRIP_FRAMES} height=${tileH} preserveAspectRatio="none" />
      </pattern>`)}</defs>
      ${runs.map((r) => html`<rect key=${r.frame} x=${r.k * tileW} width=${r.n * tileW} height=${tileH} fill=${`url(#${pat(r.frame)})`} />`)}
    </svg>`;
  }
  const path = take.thumb || (clip.source === "image" ? take.file : "");
  if (!path) return html`<div class="tl-tiles"></div>`;
  return html`<div class="tl-tiles" style=${`background-image:url("${fileUrl(pid, path)}");background-size:auto 100%`}></div>`;
}

export function VideoTrack({ project, clips, ctx, editSel, focus, picked }) {
  const transitions = useStore((s) => s.presets.transitions || []);
  const [drag, setDrag] = useState(null);
  const shown = drag && drag.clips ? drag.clips : clips;
  const { pps } = ctx;

  const beginMove = (e, clip) => {
    const id = clip.shot.id;
    const ci = clips.findIndex((c) => c.shot.id === id);
    const rest = clips.filter((c) => c.shot.id !== id);
    const home = e.currentTarget.closest(".tl-lane");
    const snap = ctx.snapper({ shot: id });
    let k = ci;
    let lift = null; // dragged onto an upper track: { target, start }
    let blocked = false; // over an upper track with no room there: dropping it changes nothing
    dragPointer(e, {
      threshold: 4,
      axis: "xy",
      onMove: (dx, ev) => {
        const hit = pipLaneAt(ev);
        if (hit && hit.target !== "video") {
          const want = round(Math.max(0, snapRange(clip.start + dx / pps, clip.length, snap, ev)), 3);
          const fit = hit.target.startsWith("layer:") ? fitGap(want, clip.length, layerSpans(project.overlays, hit.target.slice(6))) : want;
          const dy = hit.lane.getBoundingClientRect().top - home.getBoundingClientRect().top;
          if (fit == null) {
            lift = null;
            blocked = true;
            setDrag({ id, dx: (want - clip.start) * pps, dy, insert: null, blocked: true, tip: { t: want, text: BLOCKED } });
            return;
          }
          const start = round(fit, 3);
          lift = { target: hit.target, start };
          const where = hit.target === "new-layer" ? "放到新的畫面軌" : "移到這條上層軌";
          setDrag({ id, dx: (start - clip.start) * pps, dy, insert: null, tip: { t: start, text: `${where}，開始 ${start.toFixed(2)} 秒${start !== want ? "，對齊相鄰片段" : ""}，原位留空白` } });
          return;
        }
        lift = null;
        blocked = false;
        const center = clip.start + clip.length / 2 + dx / pps;
        k = rest.filter((c) => c.start + c.length / 2 < center).length;
        const at = !rest.length || k === ci ? null : k < rest.length ? rest[k].start : rest[k - 1].start + rest[k - 1].length;
        setDrag({ id, dx, insert: at });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (!moved) clickShot(e, id, clips.map((c) => c.shot.id));
        else if (cancelled || blocked) return;
        else if (lift) liftShot(project, clip, lift);
        else if (rest.length && k !== ci) moveShot(id, moveTarget(project, id, rest, k));
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
    if (!next || !(clip.transition.duration > 0) || !inView(ctx.win, next.start, clip.transition.duration)) return;
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
  const trim = ctx.tool === "trim";
  return html`${shown.filter((c) => (drag && drag.id === c.shot.id) || inView(ctx.win, c.start, c.length)).map((clip) => {
    const id = clip.shot.id;
    const geo = drag && drag.geo && drag.id === id ? drag.geo : { start: clip.start, length: clip.length, in: clip.in };
    const moving = drag && drag.dx != null && drag.id === id;
    const cls = ["tl-clip", `is-${clip.source}`, shotSel && (focus === id || picked.includes(id)) && "is-selected", moving && "is-dragging",
      moving && drag.blocked && "is-blocked", clip.shot.color && "has-color", (clip.shot.disabled || clip.shot.pictureHidden) && "is-off"].filter(Boolean).join(" ");
    const label = shotLabel(clip.shot, clip.index);
    const state = clip.shot.disabled ? "已停用，" : clip.shot.pictureHidden ? "已隱藏畫面（原聲保留），" : "";
    const style = `left:${geo.start * pps}px;width:${Math.max(2, geo.length * pps)}px`
      + (clip.shot.color ? `;--shot-color:${clip.shot.color}` : "")
      + (moving ? `;transform:translate(${drag.dx}px,${drag.dy || 0}px)` : "");
    return html`<div key=${id} class=${cls} style=${style} data-sel=${`shot:${id}`}
      title=${`${label}，${state}${fmtSeconds(clip.length, 2)}${clip.shot.notes ? `\n${clip.shot.notes}` : ""}\n${trim ? "修剪模式：拖曳左右邊緣滾動剪接點，拖曳中間滑動內容，按住 Shift 拖曳滑移鏡頭" : clipActions(ctx, "拖曳左右邊緣修剪，拖曳中間調整順序，往上拖到上層軌變成上層片段，雙擊跳到該處，右鍵看更多操作與標籤顏色", "雙擊跳到該處")}`}
      onPointerDown=${(e) => (trim ? trimDown(e, { clips, clip, side: null, ctx, setDrag }) : beginMove(e, clip))} onDblClick=${(e) => setTime(laneTime(e, pps))}>
      <${Tiles} clip=${clip} geo=${geo} pid=${project.id} pps=${pps} ar=${ctx.ar} scale=${ctx.scale} />
      <div class="tl-clip-label"><${Icon} name=${SOURCE_ICON[clip.source]} size=${12} /><span>${label}</span></div>
      ${clip.source === "empty" && !clip.shot.pictureHidden && geo.length * pps > 90 && html`<span class="tl-clip-empty">尚未產生畫面</span>`}
      <span class="tl-clip-dur mono">${fmtSeconds(geo.length, 1)}</span>
      <${Handles} onDown=${(e, side) => (trim ? trimDown(e, { clips, clip, side, ctx, setDrag }) : beginTrim(e, clip, side))} />
    </div>`;
  })}
  ${marks}
  ${drag && drag.insert != null && html`<i class="tl-insert" style=${`left:${drag.insert * pps}px`}></i>`}
  <${Tip} tip=${drag && drag.tip} pps=${pps} />`;
}

// ---------------------------------------------------------------- caption lane
// The captions on one caption row: "main" holds those whose track is missing or unknown (as vc/ops.py reads them),
// the other rows are project.captionTracks ids (components/tl-captions.js).
export function rowCaptions(project, track) {
  const ids = new Set((project.captionTracks || []).map((t) => t.id));
  return (project.captions || []).filter((c) => (track === "main" ? !ids.has(c.track) : c.track === track));
}

export function CaptionTrack({ project, ctx, editSel, track = "main" }) {
  const [drag, setDrag] = useState(null);
  const { pps } = ctx;
  const glossaryText = useStore((s) => s.settings && s.settings.glossary);
  const glossary = useMemo(() => parseGlossary(glossaryText), [glossaryText]);

  const begin = (e, cap, mode) => {
    const snap = ctx.snapper({ cap: cap.id });
    const len = cap.end - cap.start;
    // The other captions on this row: a dragged caption stops at them, unless Shift is held.
    const taken = rowCaptions(project, track).filter((c) => c.id !== cap.id).map((c) => ({ s: c.start, e: c.end }));
    const room = edgeRoom(cap.start, cap.end, taken);
    let next = null;
    dragPointer(e, {
      threshold: mode === "move" ? 3 : 1,
      onMove: (dx, ev) => {
        const shift = dx / pps;
        const free = ev.shiftKey;
        let { start, end } = cap;
        if (mode === "move") {
          const want = Math.max(0, snapRange(cap.start + shift, len, snap, ev));
          start = free ? want : fitStart(want, len, taken);
          end = start + len;
        } else if (mode === "l") {
          start = clamp(snap(cap.start + shift, ev), free ? 0 : room.lo, cap.end - MIN_ITEM);
        } else {
          end = clamp(snap(cap.end + shift, ev), cap.start + MIN_ITEM, free ? MAX_TIME : Math.min(MAX_TIME, room.hi));
        }
        next = { start: round(start, 3), end: round(end, 3) };
        let text = mode === "move" ? `${next.start.toFixed(2)} 到 ${next.end.toFixed(2)} 秒` : `長度 ${(next.end - next.start).toFixed(2)} 秒`;
        if (!free && taken.some((sp) => Math.abs(sp.e - next.start) < 1e-3 || Math.abs(sp.s - next.end) < 1e-3)) {
          text += "，碰到相鄰字幕（按住 Shift 可重疊）";
        }
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

  const near = (cap) => (drag && drag.id === cap.id) || inView(ctx.win, Number(cap.start), Number(cap.end) - Number(cap.start));
  return html`${rowCaptions(project, track).filter(near).map((cap) => {
    const cur = drag && drag.id === cap.id ? drag : cap;
    const sel = editSel && editSel.type === "caption" && editSel.id === cap.id;
    const text = (cap.text || "").trim() || "（空白字幕）";
    const member = castMember(project, cap.speaker);
    const voice = member ? voiceState(project, cap) : null;
    const typos = glossary.length ? glossaryHits(cap.text, glossary) : [];
    const cls = ["tl-cap", sel && "is-selected", drag && drag.id === cap.id && "is-dragging", voice === "stale" && "is-stale",
      cap.disabled && "is-off", typos.length && "has-typo"].filter(Boolean).join(" ");
    const who = member ? `${member.name || "未命名"}，${VOICE_STATES[voice].label}\n` : "";
    const bound = cap.shot ? project.shots.findIndex((s) => s.id === cap.shot) : -1;
    const link = bound >= 0 ? `\n綁定 ${shotLabel(project.shots[bound], bound)}` : "";
    const typo = typos.length ? `\n可能的錯字：${hitText(typos)}` : "";
    return html`<div key=${cap.id} class=${cls} data-sel=${`caption:${cap.id}`}
      style=${`left:${cur.start * pps}px;width:${Math.max(4, (cur.end - cur.start) * pps)}px`}
      title=${`${who}${text}\n${fmtSeconds(cap.start, 2)} 到 ${fmtSeconds(cap.end, 2)}，${clipActions(ctx, "拖曳碰到相鄰字幕會停住，按住 Shift 拖曳可重疊，雙擊跳到開頭", "雙擊跳到開頭")}${link}${typo}`}
      onPointerDown=${(e) => begin(e, cap, "move")} onDblClick=${() => setTime(cap.start)}>
      ${bound >= 0 && html`<${Icon} name="link" size=${11} class="tl-cap-link" />`}
      ${typos.length > 0 && html`<${Icon} name="alert" size=${11} class="tl-cap-typo" />`}
      ${member && html`<b class="tl-cap-who">${member.name || "未命名"}</b>`}
      <span class="tl-cap-text">${text}</span>
      <${Handles} onDown=${(e, side) => begin(e, cap, side)} />
    </div>`;
  })}
  <${Tip} tip=${drag && drag.tip} pps=${pps} />`;
}

// ---------------------------------------------------------------- soundtrack lane

// The audio lane under the pointer while a clip is dragged up or down: a music track, the voice rows or
// the new-track row (timeline.js marks each row data-drop="audio:<track>", "audio:new" for the new one).
function audioLaneAt(ev) {
  const lane = document.elementsFromPoint(ev.clientX, ev.clientY).find((el) => el.classList.contains("tl-lane"));
  const drop = (lane && lane.parentElement.dataset.drop) || "";
  return drop.startsWith("audio:") ? { lane, track: drop.slice(6) } : null;
}

// The clips other than `item` on music track `key`, as spans { id, s, e }.
function trackSpans(project, item, key) {
  return (project.audio || []).filter((a) => a.id !== item.id && audioTrack(a) === key)
    .map((a) => ({ id: a.id, s: Number(a.start || 0), e: Number(a.start || 0) + audioLength(a, audioMedia(project, a)) }));
}

export function AudioClip({ project, item, ctx, selected }) {
  const [drag, setDrag] = useState(null);
  const { pps } = ctx;
  const media = audioMedia(project, item);
  const full = media && media.duration ? Number(media.duration) : 0;
  // Drags work in timeline seconds; in and out are source seconds, sp of them per timeline second.
  const sp = audioSpeed(item);
  const base = { start: Number(item.start || 0), in: Number(item.in || 0), out: item.out == null ? null : Number(item.out), speed: sp };
  const cur = drag || base;
  const len = audioLength(cur, media);
  // A caption's voice-over stays on the voice rows; every other clip can be dragged onto another track.
  const free = !item.captionId;

  const begin = (e, mode) => {
    const snap = ctx.snapper({ audio: item.id });
    const a0 = base.in;
    const b0 = base.out == null ? a0 + audioLength(base, media) * sp : base.out;
    const span = (b0 - a0) / sp;
    const cap = full > 0 ? full : MAX_TIME;
    const own = audioTrack(item);
    const home = e.currentTarget.closest(".tl-lane");
    const vertical = mode === "move" && free;
    // An edge stops at the clips beside it on its music track; with Ctrl held it pushes them (and the ones beyond,
    // gaps kept) along instead. The voice rows pack themselves, so their edges never stop.
    const mates = own === VOICE_TRACK ? [] : trackSpans(project, item, own);
    const after = mates.filter((m) => m.s >= base.start + span - 1e-3);
    const before = mates.filter((m) => m.e <= base.start + 1e-3);
    const nextStart = Math.min(Infinity, ...after.map((m) => m.s));
    const prevEnd = Math.max(-Infinity, ...before.map((m) => m.e));
    const earliest = Math.min(Infinity, ...before.map((m) => m.s));
    let next = null;
    let patch = null;
    let target = null;
    let shown = false;
    let pushed = [];
    dragPointer(e, {
      threshold: mode === "move" ? 3 : 1,
      axis: vertical ? "xy" : "x",
      onMove: (dx, ev) => {
        const shift = dx / pps;
        let dy = 0;
        let text;
        if (mode === "move") {
          const want = round(Math.max(0, snapRange(base.start + shift, span, snap, ev)), 3);
          if (vertical) {
            if (!shown) {
              shown = true;
              ctx.onAudioDrag(true);
            }
            const hit = audioLaneAt(ev);
            target = hit && hit.lane !== home && hit.track !== own ? hit.track : null;
            if (target) dy = hit.lane.getBoundingClientRect().top - home.getBoundingClientRect().top;
          }
          // On a music track the clip slides into a free stretch, lined up with the clip it would run into; moved
          // over to another music track, only the free stretch under it will do.
          const dest = target || own;
          const fit = dest === VOICE_TRACK || dest === "new" ? want
            : dest === own ? fitStart(want, span, mates) : fitGap(want, span, trackSpans(project, item, dest));
          if (fit == null) {
            next = null;
            patch = null;
            setDrag({ ...base, start: want, mode, dy, blocked: true, tip: { t: want, text: BLOCKED } });
            return;
          }
          const start = round(fit, 3);
          next = { ...base, start };
          patch = { start };
          text = `開始 ${start.toFixed(2)} 秒`;
          if (start !== want) text += "，對齊相鄰片段";
          if (target === "new") text += "，放到新的音軌";
          else if (target === VOICE_TRACK) text += "，移到配音軌";
          else if (target) text += "，合併到這條音軌";
        } else if (mode === "l") {
          const s = snap(base.start + shift, ev) - base.start;
          const floor = ev.ctrlKey ? prevEnd - earliest : prevEnd; // pushed clips go back as far as 0 s
          const tIn = round(clamp(a0 + s * sp, Math.max(0, a0 - base.start * sp, a0 + (floor - base.start) * sp), b0 - MIN_ITEM * sp), 3);
          const start = round(base.start + (tIn - a0) / sp, 3);
          const gap = ev.ctrlKey ? prevEnd - start : 0;
          pushed = gap > 1e-3 ? before.map((m) => ({ id: m.id, start: round(m.s - gap, 3) })) : [];
          next = { ...base, start, in: tIn };
          patch = { start, in: tIn };
          text = `入點 ${tIn.toFixed(2)} 秒`;
          if (pushed.length) text += `，往前推 ${pushed.length} 段`;
          else if (Math.abs(start - prevEnd) < 1e-3) text += "，碰到相鄰片段（按住 Ctrl 推開）";
        } else {
          const roof = ev.ctrlKey ? cap : Math.min(cap, a0 + (nextStart - base.start) * sp);
          const tOut = round(clamp((snap(base.start + span + shift, ev) - base.start) * sp + a0, a0 + MIN_ITEM * sp, roof), 3);
          const end = base.start + (tOut - a0) / sp;
          const gap = ev.ctrlKey ? end - nextStart : 0;
          pushed = gap > 1e-3 ? after.map((m) => ({ id: m.id, start: round(m.s + gap, 3) })) : [];
          next = { ...base, out: tOut };
          patch = { out: full > 0 && tOut >= full - 1e-3 ? null : tOut };
          text = `出點 ${tOut.toFixed(2)} 秒`;
          if (pushed.length) text += `，往後推 ${pushed.length} 段`;
          else if (Math.abs(end - nextStart) < 1e-3) text += "，碰到相鄰片段（按住 Ctrl 推開）";
        }
        const t = mode === "r" ? next.start + audioLength(next, media) : next.start;
        setDrag({ ...next, mode, dy, tip: { t, text } });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (shown) ctx.onAudioDrag(false);
        if (!moved) {
          if (ev && (ev.ctrlKey || ev.metaKey)) toggleAudioSel(item.id);
          else if (ev && ev.shiftKey) rangeAudioSel(project, item);
          else setState({ editSel: { type: "audio", id: item.id }, audioSel: null });
          return;
        }
        if (cancelled || !patch) return;
        let label = mode === "move" ? "移動音軌" : mode === "l" ? "修改音軌入點" : "修改音軌出點";
        if (mode === "l") Object.assign(patch, keyPatch(item, shiftKeys(item, patch.start - base.start)));
        if (target === "new") {
          patch.track = newId("t");
          label = "移到新的音軌";
        } else if (target) {
          patch.track = target;
          label = target === VOICE_TRACK ? "移到配音軌" : "合併音軌";
        }
        const push = mode === "move" ? [] : pushed.map((m) => ({ op: "audio.update", id: m.id, patch: { start: m.start } }));
        ops([{ op: "audio.update", id: item.id, patch }, ...push], { label });
      },
    });
  };

  // The dots at the clip's top corners set its fade in or out (AU-03); a fade covers at most half the clip.
  const beginFade = (e, key) => {
    const half = audioLength(base, media) / 2;
    const f0 = Math.min(Number(item[key] || 0), half);
    const word = key === "fadeIn" ? "淡入" : "淡出";
    let value = null;
    dragPointer(e, {
      threshold: 1,
      onMove: (dx) => {
        const d = dx / pps;
        value = round(clamp(key === "fadeIn" ? f0 + d : f0 - d, 0, Math.min(30, half)), 2);
        const t = key === "fadeIn" ? base.start + value : base.start + 2 * half - value;
        setDrag({ ...base, mode: key, [key]: value, tip: { t, text: `${word} ${value.toFixed(2)} 秒` } });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        if (!moved) return setState({ editSel: { type: "audio", id: item.id }, audioSel: null });
        if (cancelled || value === null) return;
        ops([{ op: "audio.update", id: item.id, patch: { [key]: value } }], { label: `修改音軌${word}` });
      },
    });
  };
  const fading = drag && (drag.mode === "fadeIn" || drag.mode === "fadeOut") ? drag.mode : null;
  const shown = fading ? { ...item, [fading]: drag[fading] } : item;

  const width = Math.max(4, len * pps);
  const name = item.name || (media && media.name) || "音訊";
  const cls = ["tl-audio", selected && "is-selected", item.muted && "is-muted", !media && "is-missing", drag && "is-dragging",
    drag && drag.blocked && "is-blocked"].filter(Boolean).join(" ");
  const fi = Math.min(Number(shown.fadeIn || 0), len / 2) * pps;
  const fo = Math.min(Number(shown.fadeOut || 0), len / 2) * pps;
  const style = `left:${cur.start * pps}px;width:${width}px` + (drag && drag.dy ? `;transform:translateY(${drag.dy}px)` : "");
  return html`<div class=${cls} style=${style} data-sel=${`audio:${item.id}`}
    title=${`${name}\n${fmtSeconds(cur.start, 2)} 開始，長 ${fmtSeconds(len, 2)}\n${clipActions(ctx,
      `${free ? "上下拖曳換音軌，" : ""}雙擊跳到開頭，Ctrl+點擊逐一加選，Shift+點擊選取同一軌中間的片段`, "雙擊跳到開頭")}`}
    onPointerDown=${(e) => begin(e, "move")} onDblClick=${() => setTime(cur.start)}>
    ${media && html`<${Wave} pid=${project.id} media=${media} tIn=${cur.in} len=${len} speed=${sp} width=${width} item=${shown} />`}
    ${fi > 1 && html`<i class="tl-fade is-in" style=${`width:${fi}px`}></i>`}
    ${fo > 1 && html`<i class="tl-fade is-out" style=${`width:${fo}px`}></i>`}
    ${width >= 40 && html`<i class=${`tl-fade-dot is-in${fading === "fadeIn" ? " is-active" : ""}`}
        style=${`left:${Math.max(fi, 12)}px`} title="拖曳調整淡入" onPointerDown=${(e) => beginFade(e, "fadeIn")}></i>
      <i class=${`tl-fade-dot is-out${fading === "fadeOut" ? " is-active" : ""}`}
        style=${`right:${Math.max(fo, 12)}px`} title="拖曳調整淡出" onPointerDown=${(e) => beginFade(e, "fadeOut")}></i>`}
    <div class="tl-audio-label"><${Icon} name=${item.muted ? "volume-x" : "music"} size=${12} />
      <span>${media ? name : "找不到音訊檔"}</span></div>
    <${KeyMarks} item=${item} pps=${pps} drag=${drag} base=${base} length=${len} />
    ${!drag && html`<${AudioVolumeLine} item=${item} length=${len} pps=${pps} active=${selected} />`}
    <${Handles} onDown=${begin} />
    <${Tip} tip=${drag && drag.tip ? { ...drag.tip, t: drag.tip.t - cur.start } : null} pps=${pps} />
  </div>`;
}

// The picture clips' own sound, one block per clip whose video take has sound, with its waveform when the
// take has one (takes imported before waveforms were made for videos have none). It only shows the
// sound: trims and moves happen on the picture lane, and its mute switch silences this lane too.
// A click selects the shot (edit tab: "轉成字幕", volume); Ctrl adds or removes it and Shift selects the run
// of sounding clips up to it, for BatchPanel's volume.
const playsOwnSound = (c) => c.source === "video" && c.take.hasAudio && !c.shot.audioDetached;

export function hasSourceAudio(clips) {
  return clips.some(playsOwnSound);
}

export function SourceAudioTrack({ project, clips, ctx, editSel, picked }) {
  const { pps } = ctx;
  const sounding = clips.filter(playsOwnSound).map((c) => c.shot.id);
  return clips.filter((c) => playsOwnSound(c) && inView(ctx.win, c.start, c.length)).map((c) => {
    const width = Math.max(4, c.length * pps);
    const name = shotLabel(c.shot, c.index);
    const sel = (!editSel || editSel.type === "shot") && picked.includes(c.shot.id);
    const pct = Math.round(Number(c.shot.volume ?? 1) * 100);
    return html`<div key=${c.shot.id} class=${`tl-audio tl-source${sel ? " is-selected" : ""}`} data-sel=${`shot:${c.shot.id}`}
      style=${`left:${c.start * pps}px;width:${width}px`}
      title=${`${name} 的原聲，音量 ${pct}%\n${fmtSeconds(c.start, 2)} 開始，長 ${fmtSeconds(c.length, 2)}，雙擊跳到開頭\nCtrl + 點擊逐一加選或取消，Shift + 點擊選取中間所有片段，一起調原聲音量`}
      onPointerDown=${(e) => e.stopPropagation()} onDblClick=${() => setTime(c.start)}
      onClick=${(e) => clickShot(e, c.shot.id, sounding, "edit")}>
      <${Wave} pid=${project.id} media=${c.take} tIn=${c.in} len=${c.length} speed=${c.speed} width=${width} item=${c.shot}
        keys=${takeKeys(c.take)} />
      <div class="tl-audio-label"><${Icon} name="volume" size=${12} /><span>${name}${pct !== 100 ? ` ${pct}%` : ""}</span></div>
      <${KeyMarks} item=${{ keyframes: (c.take.gainKeys || []).map((k) => ({ t: (k.t - c.in) / c.speed })) }} pps=${pps}
        drag=${null} length=${c.length} />
      <${SourceVolumeLine} clip=${c} pps=${pps} active=${sel} />
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

// An overlay dropped on the picture lane becomes a new shot at `index` (dropOps keeps its placement for a lift back).
function overlayToShot(item, media, index) {
  const project = getState().project;
  const { ops: list, shot } = dropOps(project, item, media, index, takeFromMedia(media, newId("s")));
  ops(list, { label: "移到畫面軌" }).then((res) => {
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
    // The other clips on a picture track: a clip never overlaps them.
    const spansOn = (layer) => layerSpans(project.overlays, layer, item.id);
    const room = edgeRoom(base.start, base.start + base.length, spansOn(item.layer));
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
          const want = round(Math.max(0, snapRange(base.start + shift, base.length, snap, ev)), 3);
          const hit = pipLaneAt(ev);
          target = hit && hit.lane !== home && (hit.target !== "video" || media) ? hit.target : null;
          const layer = !target ? item.layer : target.startsWith("layer:") ? target.slice(6) : null;
          const fit = !layer ? want : target ? fitGap(want, base.length, spansOn(layer)) : fitStart(want, base.length, spansOn(layer));
          if (fit == null) {
            next = null;
            dy = hit.lane.getBoundingClientRect().top - home.getBoundingClientRect().top;
            setDrag({ ...base, start: want, mode, dy, insert: null, blocked: true, tip: { t: want, text: BLOCKED } });
            ctx.onPipGap(null);
            return;
          }
          const start = round(fit, 3);
          next = { ...base, start };
          text = `開始 ${start.toFixed(2)} 秒`;
          if (start !== want) text += "，對齊相鄰片段";
          if (target) {
            dy = hit.lane.getBoundingClientRect().top - home.getBoundingClientRect().top;
            if (target === "video") {
              insert = ctx.insertAt(start);
              text = "穿插到這裡，後面的鏡頭往後退";
            } else text += target === "new-layer" ? "，放到新的畫面軌" : "，換到這條軌道";
          }
        } else if (mode === "l") {
          const end = base.start + base.length;
          const lo = Math.max(room.lo, still ? end - MAX_STILL : base.start - base.in);
          const start = round(clamp(snap(base.start + shift, ev), lo, end - minLen), 3);
          const tIn = still ? base.in : round(base.in + start - base.start, 3);
          next = { start, in: tIn, length: round(end - start, 3) };
          text = still ? `長度 ${next.length.toFixed(2)} 秒` : `入點 ${tIn.toFixed(2)} 秒`;
          if (room.lo > 0 && Math.abs(start - room.lo) < 1e-3) text += "，碰到相鄰片段";
        } else {
          const hi = Math.min(room.hi - base.start, still ? MAX_STILL : full > 0 ? full - base.in : MAX_TIME);
          const length = round(clamp(snap(base.start + base.length + shift, ev) - base.start, minLen, hi), 3);
          next = { ...base, length };
          text = `長度 ${length.toFixed(2)} 秒`;
          if (Math.abs(base.start + length - room.hi) < 1e-3) text += "，碰到相鄰片段";
        }
        const t = insert ? insert.at : mode === "r" ? next.start + next.length : next.start;
        setDrag({ ...next, mode, dy, insert, tip: { t, text } });
        if (mode === "move") ctx.onPipGap(insert && { at: insert.at, length: base.length });
      },
      onEnd: (moved, ev, cancelled) => {
        setDrag(null);
        ctx.onPipGap(null);
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
        if (mode === "l" && patch.start != null) Object.assign(patch, keyPatch(item, shiftKeys(item, patch.start - base.start)));
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
    drag && "is-dragging", drag && drag.blocked && "is-blocked", item.disabled && "is-off"].filter(Boolean).join(" ");
  const style = `left:${cur.start * pps}px;width:${width}px` + (drag && drag.dy ? `;transform:translateY(${drag.dy}px)` : "");
  return html`<div class=${cls} style=${style} data-sel=${`overlay:${item.id}`}
    title=${`${name}\n${fmtSeconds(cur.start, 2)} 開始，長 ${fmtSeconds(cur.length, 2)}\n${clipActions(ctx,
      "拖曳移動，上下拖曳換軌道，拖進「畫面」軌可穿插成鏡頭，拖曳左右邊緣修剪，雙擊跳到開頭", "雙擊跳到開頭")}`}
    onPointerDown=${(e) => begin(e, "move")} onDblClick=${() => setTime(cur.start)}>
    ${thumb && html`<div class="tl-tiles" style=${`background-image:url("${fileUrl(project.id, thumb)}");background-size:auto 100%`}></div>`}
    <div class="tl-clip-label"><${Icon} name=${still ? "image" : "video"} size=${12} /><span>${media ? name : "找不到素材檔"}</span></div>
    <span class="tl-clip-dur mono">${fmtSeconds(cur.length, 1)}</span>
    <${KeyMarks} item=${item} pps=${pps} drag=${drag} base=${base} length=${cur.length} />
    <${Handles} onDown=${begin} />
  </div>
  ${drag && drag.insert && html`<i class="tl-insert"
    style=${`left:${drag.insert.at * pps}px;top:${drag.dy}px;bottom:auto;height:${VIDEO_H}px`}></i>`}
  <${Tip} tip=${drag && drag.tip} pps=${pps} />`;
}
