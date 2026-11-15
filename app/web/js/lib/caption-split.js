// 拆成下一句: the split key in a caption's text box (Ctrl+Enter) cuts the line at the text cursor. The words before
// the cursor stay in the caption, the words after it become a new caption on the same row, and the time cut is the
// playhead, as Alt+B cuts a caption there. Pure, so tests run it in node.
import { round } from "./format.js";

const MIN_LEN = 0.1; // the shortest caption a cut leaves, as vc/edits.py MIN_ITEM

// The KTV wipe marks (vc/ktv.py) of the words from index cut of text on, moved by shift seconds to the new caption's
// start. Only when the box holds the saved text and the marks fit it; the new caption otherwise has no wipe. The
// caption keeps its own marks: the server carries them over when its text changes (ktv.follow).
function tailMarks(cap, text, cut, shift) {
  const n = [...text.trim()].length;
  if (text !== cap.text || !Array.isArray(cap.kt) || cap.kt.length !== n + 1) return null;
  const lead = text.length - text.trimStart().length;
  const k = [...text.slice(lead, cut)].length;
  return cap.kt.slice(k).map((m) => round(Math.max(0, Number(m) - shift), 3));
}

// cap: the caption; text: what its box holds (maybe not saved yet); from, to: the selection in the box, which the
// cut drops as Enter would; t: the playhead; id: the new caption's id. Returns {ops, id} or {error}.
export function splitTextTarget(cap, text, from, to, t, id) {
  const head = text.slice(0, from).trimEnd();
  const after = text.slice(to);
  const tail = after.trimStart();
  if (!head.trim() || !tail.trim()) return { error: "游標前後都要有文字才能拆句" };
  const start = Number(cap.start);
  const end = Number(cap.end);
  if (t <= start || t >= end) return { error: "播放頭不在這句字幕裡，請先把播放頭停在要拆開的位置" };
  if (t - start < MIN_LEN || end - t < MIN_LEN) return { error: "播放頭太靠近這句字幕的開頭或結尾" };
  const at = round(t, 3);
  const { kt: _kt, ...fields } = cap;
  const marks = tailMarks(cap, text, to + after.length - tail.length, at - start);
  const item = { ...fields, id, start: at, text: tail, shot: null, ...(marks ? { kt: marks } : {}) };
  return {
    id,
    ops: [{ op: "caption.update", id: cap.id, patch: { text: head, end: at } }, { op: "caption.add", item }],
  };
}
