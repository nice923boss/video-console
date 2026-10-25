// Playhead edits on the edit timeline: split the clip under the playhead in two, or trim away the part
// before or after it. The selection picks the track (the picture lane when nothing or a shot is
// selected) and the playhead picks the clip on that track. On the picture lane the shots after a trimmed
// one close up, on the other tracks the trimmed edge moves to the playhead. The server works out the
// edit (POST /edit, vc/edits.py), the same one Claude's edits get, and refuses a playhead too near an edge.
import { editCmd } from "../lib/actions.js";
import { clock, setTime } from "../lib/clock.js";
import { round } from "../lib/format.js";
import { clipIndexAt, audioLength, audioMedia, audioTrack } from "../lib/sequence.js";
import { toast } from "./toast.js";

const inside = (start, length, t) => t > start && t < start + length;

// Among the items on the selected track that cover t, the selected one wins.
function pick(items, sel, span, t) {
  const hits = items.filter((it) => inside(...span(it), t));
  return hits.find((it) => it.id === sel.id) || hits[0] || null;
}

// The clip a playhead edit acts on: {type, id, start}, or {missing: text}.
function targetAt(project, clips, sel, t) {
  const overlay = sel && sel.type === "overlay" && (project.overlays || []).find((o) => o.id === sel.id);
  const audio = sel && sel.type === "audio" && (project.audio || []).find((a) => a.id === sel.id);
  const caption = sel && sel.type === "caption" && (project.captions || []).some((c) => c.id === sel.id);
  const adjust = sel && sel.type === "adjust" && (project.adjustments || []).some((j) => j.id === sel.id);
  let item = null;
  if (overlay) {
    const span = (o) => [Number(o.start || 0), Number(o.length || 0.1)];
    item = pick((project.overlays || []).filter((o) => o.layer === overlay.layer), sel, span, t);
  } else if (audio) {
    const span = (a) => [Number(a.start || 0), audioLength(a, audioMedia(project, a))];
    item = pick((project.audio || []).filter((a) => audioTrack(a) === audioTrack(audio)), sel, span, t);
  } else if (caption) {
    item = pick(project.captions || [], sel, (c) => [Number(c.start || 0), Number(c.end || 0) - Number(c.start || 0)], t);
  } else if (adjust) {
    item = pick(project.adjustments || [], sel, (j) => [Number(j.start || 0), Number(j.length || 0.1)], t);
  } else {
    const clip = clips[clipIndexAt(clips, t)];
    if (!clip) return { missing: "播放頭下方沒有片段" };
    return { type: "shot", id: clip.shot.id, start: clip.start };
  }
  return item ? { type: sel.type, id: item.id } : { missing: "選取的軌道在播放頭位置沒有片段" };
}

// The target, or null after telling the viewer why there is none.
function resolve(project, clips, sel) {
  const target = targetAt(project, clips, sel, clock.time);
  if (!target.missing) return target;
  toast(target.missing, { level: "warn" });
  return null;
}

export function splitAtPlayhead(project, clips, sel) {
  const g = resolve(project, clips, sel);
  if (g) editCmd({ cmd: "split", t: round(clock.time, 3), type: g.type, id: g.id });
}

// Cut every clip on every track that the playhead crosses, as one undo step. Clips too close to an
// edge at the playhead are left whole.
export function splitAllAtPlayhead() {
  editCmd({ cmd: "splitAll", t: round(clock.time, 3) });
}

// side "start" removes the part before the playhead, "end" the part after it.
export function trimAtPlayhead(project, clips, sel, side) {
  const g = resolve(project, clips, sel);
  if (!g) return;
  editCmd({ cmd: "trim", t: round(clock.time, 3), side, type: g.type, id: g.id }).then((res) => {
    // The shots after it move up, so the playhead goes to where the kept part now starts.
    if (res && g.type === "shot" && side === "start") setTime(g.start);
  });
}
