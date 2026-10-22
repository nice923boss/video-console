// Playhead edits on the edit timeline: split the clip under the playhead in two, or trim away the part
// before or after it. The selection picks the track (the picture lane when nothing or a shot is
// selected) and the playhead picks the clip on that track. On the picture lane the shots after a trimmed
// one close up, on the other tracks the trimmed edge moves to the playhead.
import { ops } from "../lib/actions.js";
import { clock, setTime } from "../lib/clock.js";
import { newId, round } from "../lib/format.js";
import { clipIndexAt, audioLength } from "../lib/sequence.js";
import { toast } from "./toast.js";

const MIN_VIDEO = 0.1;
const MIN_STILL = 0.2;
const MIN_ITEM = 0.1;

const inside = (start, length, t) => t > start && t < start + length;

// Among the items on the selected track that cover t, the selected one wins.
function pick(items, sel, span, t) {
  const hits = items.filter((it) => inside(...span(it), t));
  return hits.find((it) => it.id === sel.id) || hits[0] || null;
}

// The clip a playhead edit acts on: {type, item, start, length, min, ...}, or {missing: text}.
function targetAt(project, clips, sel, t) {
  const media = project.media || {};
  const overlay = sel && sel.type === "overlay" && (project.overlays || []).find((o) => o.id === sel.id);
  const audio = sel && sel.type === "audio" && (project.audio || []).find((a) => a.id === sel.id);
  const caption = sel && sel.type === "caption" && (project.captions || []).some((c) => c.id === sel.id);
  const adjust = sel && sel.type === "adjust" && (project.adjustments || []).some((j) => j.id === sel.id);
  const missing = { missing: "選取的軌道在播放頭位置沒有片段" };
  if (overlay) {
    const span = (o) => [Number(o.start || 0), Number(o.length || MIN_ITEM)];
    const item = pick((project.overlays || []).filter((o) => o.layer === overlay.layer), sel, span, t);
    if (!item) return missing;
    const still = (media[item.mediaId] || {}).kind === "image";
    const [start, length] = span(item);
    return { type: "overlay", item, start, length, still, min: still ? MIN_STILL : MIN_ITEM };
  }
  if (audio) {
    const span = (a) => [Number(a.start || 0), audioLength(a, media[a.mediaId])];
    const track = audio.captionId ? (project.audio || []).filter((a) => a.captionId) : [audio];
    const item = pick(track, sel, span, t);
    if (!item) return missing;
    const [start, length] = span(item);
    return { type: "audio", item, start, length, min: MIN_ITEM };
  }
  if (caption) {
    const span = (c) => [Number(c.start || 0), Number(c.end || 0) - Number(c.start || 0)];
    const item = pick(project.captions || [], sel, span, t);
    if (!item) return missing;
    const [start, length] = span(item);
    return { type: "caption", item, start, length, min: MIN_ITEM };
  }
  if (adjust) {
    const span = (j) => [Number(j.start || 0), Number(j.length || MIN_ITEM)];
    const item = pick(project.adjustments || [], sel, span, t);
    if (!item) return missing;
    const [start, length] = span(item);
    return { type: "adjust", item, start, length, min: MIN_ITEM };
  }
  const clip = clips[clipIndexAt(clips, t)];
  if (!clip) return { missing: "播放頭下方沒有片段" };
  return { type: "shot", clip, start: clip.start, length: clip.length, min: clip.source === "video" ? MIN_VIDEO : MIN_STILL };
}

// The target and the playhead's offset into it, or null after telling the viewer why not.
function resolve(project, clips, sel) {
  const t = clock.time;
  const target = targetAt(project, clips, sel, t);
  if (target.missing) {
    toast(target.missing, { level: "warn" });
    return null;
  }
  const local = t - target.start;
  if (local < target.min || target.length - local < target.min) {
    toast("播放頭太靠近片段邊緣，無法在這裡剪", { level: "warn" });
    return null;
  }
  return { ...target, t: round(t, 3), local };
}

function splitShot({ clip, local }) {
  const { shot } = clip;
  const trans = shot.transition || { type: "cut", duration: 0.5 };
  const head = clip.source === "video" ? { trimOut: round(clip.in + local, 3) } : { duration: round(local, 2) };
  const tail = clip.source === "video"
    ? { trimIn: round(clip.in + local, 3), trimOut: shot.trimOut ?? null }
    : { duration: round(clip.length - local, 2) };
  const copy = { ...shot, ...tail, id: newId("s") };
  return ops([
    { op: "shot.update", id: shot.id, patch: { ...head, transition: { ...trans, type: "cut" } } },
    { op: "shot.add", shot: copy, index: clip.index + 1 },
  ], { label: "分割鏡頭" });
}

export function splitAtPlayhead(project, clips, sel) {
  const g = resolve(project, clips, sel);
  if (!g) return;
  if (g.type === "shot") {
    splitShot(g);
    return;
  }
  const { item, t, local } = g;
  if (g.type === "overlay") {
    const tail = { ...item, id: newId("o"), start: t, in: g.still ? item.in : round(Number(item.in || 0) + local, 3),
      length: round(g.length - local, 3) };
    ops([
      { op: "overlay.update", id: item.id, patch: { length: round(local, 3) } },
      { op: "overlay.add", item: tail },
    ], { label: "分割片段" });
  } else if (g.type === "audio") {
    const cut = round(Number(item.in || 0) + local, 3);
    ops([
      { op: "audio.update", id: item.id, patch: { out: cut, fadeOut: 0 } },
      { op: "audio.add", item: { ...item, id: newId("a"), start: t, in: cut, fadeIn: 0 } },
    ], { label: "分割音軌" });
  } else if (g.type === "adjust") {
    ops([
      { op: "adjust.update", id: item.id, patch: { length: round(local, 3) } },
      { op: "adjust.add", item: { ...item, id: newId("adj"), start: t, length: round(g.length - local, 3) } },
    ], { label: "分割調整圖層" });
  } else {
    ops([
      { op: "caption.update", id: item.id, patch: { end: t } },
      { op: "caption.add", item: { ...item, id: newId("c"), start: t, shot: null } },
    ], { label: "分割字幕" });
  }
}

// side "start" removes the part before the playhead, "end" the part after it.
export function trimAtPlayhead(project, clips, sel, side) {
  const g = resolve(project, clips, sel);
  if (!g) return;
  const { item, t, local } = g;
  const left = side === "start";
  const label = left ? "向左裁剪" : "向右裁剪";
  if (g.type === "shot") {
    const { clip } = g;
    let patch;
    if (clip.source === "video") patch = left ? { trimIn: round(clip.in + local, 3) } : { trimOut: round(clip.in + local, 3) };
    else patch = { duration: round(left ? clip.length - local : local, 2) };
    // The shots after it move up, so the playhead goes to where the kept part now starts.
    ops([{ op: "shot.update", id: clip.shot.id, patch }], { label }).then((res) => {
      if (res && left) setTime(clip.start);
    });
    return;
  }
  let patch;
  if (g.type === "overlay") {
    const tIn = g.still ? item.in : round(Number(item.in || 0) + local, 3);
    patch = left ? { start: t, in: tIn, length: round(g.length - local, 3) } : { length: round(local, 3) };
  } else if (g.type === "audio") {
    const cut = round(Number(item.in || 0) + local, 3);
    patch = left ? { start: t, in: cut } : { out: cut };
  } else if (g.type === "adjust") {
    patch = left ? { start: t, length: round(g.length - local, 3) } : { length: round(local, 3) };
  } else {
    patch = left ? { start: t } : { end: t };
  }
  ops([{ op: `${g.type}.update`, id: item.id, patch }], { label });
}
