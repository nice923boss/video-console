// 延長到下一句: a caption's new end is the start of the next caption on its row (the main 字幕 row, or its caption
// track), so the gap between the two lines closes, or an overlap goes. E does it for the selected caption
// (extendTarget), the row header button does every caption on one row at once (extendAllTargets). Shift+F1 and Shift+F3 trim the caption
// under the playhead and bring the neighbour on that side up to the cut (trimJoinTarget). Pure, so tests run it in node.
import { round } from "./format.js";

const EPS = 1e-3;
const MIN_LEN = 0.1; // the shortest caption a trim leaves, as F1 and F3 (vc/edits.py MIN_ITEM)
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
  if (Math.abs(next - end) <= EPS) return { error: "這句已經接到下一句" };
  // Overlapping the next caption: the end comes back to where the next one starts (shortened is true).
  if (next < end) {
    if (next - start < MIN_LEN) return { error: "下一句幾乎和這句同時開始，縮短後這句會太短" };
    return { end: round(next, 3), shortened: true };
  }
  return { end: round(next, 3) };
}

// 裁剪並接上鄰句: F1 or F3 on the caption under the playhead t, then its neighbour on the row meets the cut, as one
// list of caption.update ops. side "start" trims the part before t and the previous caption ends at t; "end"
// trims the part after t and the next caption starts at t. The row is the selected caption's (id), else the main
// row. Returns {ops, joined} (joined false when no caption sits on that side), or {error}.
export function trimJoinTarget(project, id, t, side) {
  const caps = project.captions || [];
  const row = rowOf(project);
  const track = captionRow(project, id);
  const hits = caps.filter((c) => row(c) === track && num(c.start) < t && t < num(c.end));
  const cap = hits.find((c) => c.id === id) || hits[0];
  if (!cap) return { error: "播放頭下方沒有字幕" };
  const start = num(cap.start);
  if (t - start < MIN_LEN || num(cap.end) - t < MIN_LEN) return { error: "播放頭太靠近這句字幕的開頭或結尾" };
  const left = side === "start";
  const at = round(t, 3);
  const ops = [{ op: "caption.update", id: cap.id, patch: left ? { start: at } : { end: at } }];
  // The previous caption is the one that starts last before this one, the next the one that starts first after it.
  const dir = left ? -1 : 1;
  const near = caps.filter((c) => c.id !== cap.id && row(c) === track && dir * (num(c.start) - start) > EPS)
    .reduce((a, c) => (!a || dir * (num(c.start) - num(a.start)) < 0 ? c : a), null);
  if (!near) return { ops, joined: false };
  if (!left && num(near.end) - t < MIN_LEN) return { error: "下一句在播放頭之前就結束了，無法接到播放頭" };
  const field = left ? "end" : "start";
  if (Math.abs(num(near[field]) - t) > EPS) ops.push({ op: "caption.update", id: near.id, patch: { [field]: at } });
  return { ops, joined: true };
}

// Every caption on row track ("main" or a caption track id) that E would change, in start order, as [{id, end}]
// (shortened: true on one that overlapped the next). Captions already touching the next, and the last, stay out.
export function extendAllTargets(project, track) {
  const row = rowOf(project);
  return (project.captions || []).filter((c) => row(c) === track)
    .sort((a, b) => num(a.start) - num(b.start))
    .flatMap((c) => {
      const to = extendTarget(project, c.id);
      if (to.error) return [];
      return [to.shortened ? { id: c.id, end: to.end, shortened: true } : { id: c.id, end: to.end }];
    });
}
