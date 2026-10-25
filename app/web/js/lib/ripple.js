// Ripple delete (Shift+Delete) of a clip above the picture lane: the clip goes and what comes after it on the
// same track moves back by its length. A caption and its voice-over count as one: they go and move together.
import { round } from "./format.js";
import { VOICE_TRACK, audioLength, audioTrack } from "./sequence.js";

const LISTS = { caption: "captions", audio: "audio", overlay: "overlays", adjust: "adjustments" };

function span(project, type, item) {
  const start = Number(item.start || 0);
  if (type === "caption") return [start, Number(item.end || 0)];
  if (type === "audio") return [start, start + audioLength(item, project.media[item.mediaId])];
  return [start, start + Number(item.length || 0)];
}

// A caption's voice-overs, or a voice-over's caption.
function partners(project, type, item) {
  if (type === "caption") return (project.audio || []).filter((a) => a.captionId === item.id).map((x) => ["audio", x]);
  if (type === "audio" && item.captionId) {
    return (project.captions || []).filter((c) => c.id === item.captionId).map((x) => ["caption", x]);
  }
  return [];
}

// The ops for ripple-deleting `sel` ({type, id}), or null when it isn't a clip that can.
export function rippleOps(project, sel) {
  const key = sel && LISTS[sel.type];
  const item = key && (project[key] || []).find((x) => x.id === sel.id);
  if (!item) return null;
  const [start, end] = span(project, sel.type, item);
  const sameTrack = {
    caption: () => true,
    audio: (x) => audioTrack(x) === audioTrack(item),
    overlay: (x) => x.layer === item.layer,
    adjust: () => true,
  }[sel.type];
  const gone = [[sel.type, item], ...partners(project, sel.type, item)];
  const out = gone.map(([type, x]) => ({ op: `${type}.delete`, id: x.id }));
  const done = new Set(gone.map(([, x]) => x.id));
  const shift = (type, x, d) => {
    if (done.has(x.id)) return;
    done.add(x.id);
    const s = round(Number(x.start || 0) + d, 3);
    const patch = type === "caption" ? { start: s, end: round(s + Number(x.end) - Number(x.start || 0), 3) } : { start: s };
    out.push({ op: `${type}.update`, id: x.id, patch });
  };
  const later = (project[key] || []).filter((x) => !done.has(x.id) && Number(x.start || 0) >= start - 1e-6 && sameTrack(x));
  for (const x of later) {
    const s = Number(x.start || 0);
    const d = Math.max(start, s - (end - start)) - s;
    if (Math.abs(d) < 1e-6) continue;
    shift(sel.type, x, d);
    if (sel.type === "caption" || audioTrack(x) === VOICE_TRACK) {
      for (const [type, y] of partners(project, sel.type, x)) shift(type, y, d);
    }
  }
  return out;
}
