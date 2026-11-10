// 延長到下一句: a caption's new end is the start of the next caption on its row (the main 字幕 row, or its caption
// track), so the gap between the two lines closes. E does it for the selected caption (extendTarget), the row
// header button for every caption on one row at once (extendAllTargets). Pure, so tests run it in node.
import { round } from "./format.js";

const EPS = 1e-3;
const num = (v) => Number(v || 0);

// The row each caption shows on: its caption track, or "main" (no track, or a track that no longer exists).
function rowOf(project) {
  const tracks = new Set((project.captionTracks || []).map((t) => t.id));
  return (c) => (tracks.has(c.track) ? c.track : "main");
}

// The row of caption id, or "main" when there is no such caption.
export function captionRow(project, id) {
  const cap = (project.captions || []).find((c) => c.id === id);
  return cap ? rowOf(project)(cap) : "main";
}

export function extendTarget(project, id) {
  const caps = project.captions || [];
  const cap = caps.find((c) => c.id === id);
  if (!cap) return { error: "請先選取一則字幕" };
  const row = rowOf(project);
  const start = num(cap.start);
  const end = num(cap.end);
  const next = Math.min(Infinity, ...caps
    .filter((c) => c.id !== id && row(c) === row(cap) && num(c.start) > start + EPS)
    .map((c) => num(c.start)));
  if (next === Infinity) return { error: "這句後面沒有字幕可以對齊" };
  if (next < end - EPS) return { error: "這句和下一句已經重疊" };
  if (next <= end + EPS) return { error: "這句已經接到下一句" };
  return { end: round(next, 3) };
}

// Every caption on row track ("main" or a caption track id) that a gap parts from the next one, as [{id, end}]
// with the end that closes the gap. Captions already touching or overlapping the next, and the last, stay out.
export function extendAllTargets(project, track) {
  const row = rowOf(project);
  const mine = (project.captions || []).filter((c) => row(c) === track)
    .sort((a, b) => num(a.start) - num(b.start));
  return mine.flatMap((c, i) => {
    let j = i + 1;
    while (j < mine.length && num(mine[j].start) <= num(c.start) + EPS) j += 1;
    const next = j < mine.length ? num(mine[j].start) : -Infinity;
    return next > num(c.end) + EPS ? [{ id: c.id, end: round(next, 3) }] : [];
  });
}
