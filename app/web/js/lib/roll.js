// Mirror of the roll, slip and slide limits in app/vc/trimtool.py, so the trim tool's drag stops where the server
// would. The server makes the ops (POST /edit); this only shapes the preview.
const MIN_VIDEO = 0.1;
const MIN_STILL = 0.2;
const MAX_STILL = 600;

// How far (timeline seconds) a clip's end can move earlier and later while its first frame stays.
function endRoom(clip) {
  if (clip.source === "video") return [clip.length - MIN_VIDEO / clip.speed, (Number(clip.take.duration) - clip.out) / clip.speed];
  return [clip.length - MIN_STILL, MAX_STILL - clip.length];
}

// How far a clip's first frame can move later and earlier while its last frame stays.
function headRoom(clip) {
  if (clip.source === "video") return [clip.length - MIN_VIDEO / clip.speed, clip.in / clip.speed];
  return [clip.length - MIN_STILL, MAX_STILL - clip.length];
}

// How far the cut between two clips can move earlier (negative) and later.
export const cutRange = (before, after) => [
  -Math.max(0, Math.min(endRoom(before)[0], headRoom(after)[1])),
  Math.max(0, Math.min(endRoom(before)[1], headRoom(after)[0])),
];

// How far a video clip's footage can slip (positive shows later footage).
export const slipRange = (clip) => [-clip.in / clip.speed, Math.max(0, (Number(clip.take.duration) - clip.out) / clip.speed)];

export const clampTo = (v, [lo, hi]) => Math.min(Math.max(v, lo), hi);

// The clips as the timeline draws them while the cut after clips[i] moves d seconds (transitions left as they were).
export function movedCut(clips, i, j, d) {
  return clips.map((c, k) => {
    if (k === i) return { ...c, length: c.length + d, out: c.out + d * c.speed };
    if (k === j) return { ...c, start: c.start + d, length: c.length - d, in: c.in + d * c.speed };
    if (k > i && k < j) return { ...c, start: c.start + d };
    return c;
  });
}
