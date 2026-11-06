// Speed of an upper-track video clip. The clip keeps its in point and plays the same piece of its file, so its
// length on the timeline changes by old / new speed; it stops short of the next clip on its track and of the
// file's end. Keyframes (seconds into the clip) stretch with it, as when linked edits stretch a clip.
import { round } from "./format.js";
import { clipSpeed } from "./sequence.js";
import { edgeRoom, layerSpans } from "./track-fit.js";
import { splitKeys } from "./keyframes.js";

export const SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4];
const MIN_LEN = 0.1;

// Select options for a speed, with the current one added when it is not a preset.
export const speedOptions = (speed) => (SPEEDS.includes(speed) ? SPEEDS : [...SPEEDS, speed].sort((a, b) => a - b))
  .map((v) => ({ value: v, label: `${v}x` }));

// { patch, cut } for overlay.update: cut is "next" (the next clip on the track) or "end" (the file's end) when
// the clip came out shorter than its piece of the file at the new speed, else null.
export function overlaySpeedPatch(project, item, media, speed) {
  const old = clipSpeed(item, "video");
  const next = clipSpeed({ speed }, "video");
  const start = Number(item.start || 0);
  const len = Number(item.length || MIN_LEN);
  const want = (len * old) / next;
  const room = edgeRoom(start, start + len, layerSpans(project.overlays, item.layer, item.id)).hi - start;
  const full = Number(media && media.duration) || 0;
  const fileRoom = full > 0 ? (full - Number(item.in || 0)) / next : Infinity;
  const length = round(Math.max(MIN_LEN, Math.min(want, room, fileRoom)), 3);
  const cut = length < round(want, 3) - 1e-3 ? (room <= fileRoom ? "next" : "end") : null;
  const keys = item.keyframes || [];
  let keyframes = keys.map((k) => ({ ...k, t: round((Number(k.t) * old) / next, 3) }));
  if (cut && keyframes.some((k) => k.t > length + 1e-3)) keyframes = splitKeys({ keyframes }, length)[0];
  return { patch: { speed: next, length, ...(keys.length ? { keyframes } : {}) }, cut };
}
