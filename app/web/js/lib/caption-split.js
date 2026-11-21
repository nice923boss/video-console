// 拆成下一句, 合併到上一句, 移到下一句開頭 and 移到上一句結尾: the split key in a caption's text box (Alt+Enter) cuts the line at the text cursor. The words before
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

// 合併到上一句: the merge key in a caption's text box (Ctrl+Enter) undoes a split. The box's words go on the
// end of the previous caption on the same row (the one that starts last before this one, title cards aside), that caption now runs to
// this one's end, and this one goes. The KTV wipe carries over when both lines' marks fit their text; a wipe that
// cannot cover the merged words is dropped rather than left to flash them at once.
const sameRow = (project) => {
  const tracks = new Set((project.captionTracks || []).map((t) => t.id));
  return (c) => (tracks.has(c.track) ? c.track : "main");
};

const fits = (c, t) => {
  const n = [...(t || "").trim()].length;
  return n > 0 && Array.isArray(c.kt) && c.kt.length === n + 1;
};

// The merged line's marks; undefined leaves the previous caption's as they are (no wipe, or no words added).
function mergedMarks(prev, cap, text, shift) {
  if (!fits(prev, prev.text) || !text.trim()) return undefined;
  if (text !== cap.text || !fits(cap, text)) return null;
  const head = prev.kt.map(Number);
  // the joining space starts where the previous line's wipe ends; this line's marks move by shift seconds
  return [...head, ...cap.kt.map((m) => round(Number(m) + shift, 3))];
}

// project: the project; cap: the caption; text: what its box holds (maybe not saved yet). Returns {ops, id} (id:
// the previous caption, which the panel moves to) or {error}.
export function mergeTextTarget(project, cap, text) {
  if (cap.title) return { error: "標題字卡不能合併到上一句" };
  const row = sameRow(project);
  const start = Number(cap.start);
  const prev = (project.captions || [])
    .filter((c) => c.id !== cap.id && !c.title && row(c) === row(cap) && Number(c.start) < start - 1e-3)
    .reduce((a, c) => (!a || Number(c.start) > Number(a.start) ? c : a), null);
  if (!prev) return { error: "這句前面沒有字幕可以合併" };
  const head = (prev.text || "").trimEnd();
  const tail = text.trim();
  const joined = head.trim() && tail ? `${head} ${tail}` : head + tail;
  const end = round(Math.max(Number(prev.end), Number(cap.end)), 3);
  const marks = mergedMarks(prev, cap, text, start - Number(prev.start));
  const patch = { text: joined, end, ...(marks !== undefined ? { kt: marks } : {}) };
  return {
    id: prev.id,
    ops: [{ op: "caption.update", id: prev.id, patch }, { op: "caption.delete", id: cap.id }],
  };
}

// 移到下一句開頭: the move key in a caption's text box (Alt+Shift+Enter) fixes a line break one word too late. The words
// after the text cursor go on the front of the next caption on the same row, and the time cut is the playhead, as
// Shift+F3: this caption ends there and the next one starts there. The KTV wipe carries over when both lines' marks
// fit their text; the next line's wipe is dropped when it cannot cover the words it gains.
const latin = /[A-Za-z0-9]/;

function movedMarks(cap, next, text, k, t, gap) {
  if (!fits(next, next.text)) return undefined;
  if (text !== cap.text || !fits(cap, text)) return null;
  const s0 = Number(cap.start);
  const s1 = Number(next.start);
  const n = cap.kt.length - 1;
  const moved = cap.kt.slice(k, n).map((m) => round(Math.max(0, Number(m) + s0 - t), 3));
  const rest = next.kt.map((m) => round(Number(m) + s1 - t, 3));
  return [...moved, ...(gap ? [rest[0]] : []), ...rest]; // a joining space starts with the next line's first word
}

// project: the project; cap: the caption; text: what its box holds (maybe not saved yet); from, to: the selection in
// the box, which the move drops as Enter would; t: the playhead. Returns {ops, id} (id: the next caption) or {error}.
export function moveTailTarget(project, cap, text, from, to, t) {
  if (cap.title) return { error: "標題字卡不能把文字移到下一句" };
  const head = text.slice(0, from).trimEnd();
  const after = text.slice(to);
  const tail = after.trimStart();
  if (!head.trim() || !tail.trim()) return { error: "游標前後都要有文字，才能把後面的字移到下一句" };
  const row = sameRow(project);
  const start = Number(cap.start);
  const next = (project.captions || [])
    .filter((c) => c.id !== cap.id && !c.title && row(c) === row(cap) && Number(c.start) > start + 1e-3)
    .reduce((a, c) => (!a || Number(c.start) < Number(a.start) ? c : a), null);
  if (!next) return { error: "這句後面沒有字幕可以接" };
  if (t <= start || t >= Number(cap.end)) return { error: "播放頭不在這句字幕裡，請先把播放頭停在要切開的位置" };
  if (t - start < MIN_LEN || Number(cap.end) - t < MIN_LEN) return { error: "播放頭太靠近這句字幕的開頭或結尾" };
  if (Number(next.end) - t < MIN_LEN) return { error: "下一句在播放頭之前就結束了，無法接到播放頭" };
  const at = round(t, 3);
  const body = (next.text || "").trimStart();
  const words = tail.trimEnd();
  const gap = latin.test(words.slice(-1)) && latin.test(body.slice(0, 1)) ? " " : "";
  const lead = text.length - text.trimStart().length;
  const k = [...text.slice(lead, to + after.length - tail.length)].length;
  const marks = movedMarks(cap, next, text, k, at, gap);
  const patch = { text: words + gap + body, start: at, ...(marks !== undefined ? { kt: marks } : {}) };
  return {
    id: next.id,
    ops: [{ op: "caption.update", id: cap.id, patch: { text: head, end: at } },
      { op: "caption.update", id: next.id, patch }],
  };
}

// 移到上一句結尾: the mirror key (Ctrl+Shift+Enter) fixes a line break one word too early. The words before the text
// cursor go on the end of the previous caption on the same row, and the time cut is the playhead, as Shift+F1: this
// caption starts there and the previous one ends there. The KTV wipe carries over when both lines' marks fit their
// text; the previous line's wipe is dropped when it cannot cover the words it gains.
function headMarks(prev, cap, text, m, gap) {
  if (!fits(prev, prev.text)) return undefined;
  if (text !== cap.text || !fits(cap, text)) return null;
  const n = prev.kt.length - 1;
  const moved = cap.kt.slice(0, m + 1).map((x) => round(Number(x) + Number(cap.start) - Number(prev.start), 3));
  return [...prev.kt.slice(0, gap ? n + 1 : n).map(Number), ...moved]; // the old end mark starts a joining space
}

// project, cap, text, from, to as moveTailTarget; t: the playhead. Returns {ops, id} (id: this caption) or {error}.
export function moveHeadTarget(project, cap, text, from, to, t) {
  if (cap.title) return { error: "標題字卡不能把文字移到上一句" };
  const words = text.slice(0, from).trim();
  const after = text.slice(to);
  const rest = after.trimStart();
  if (!words || !rest.trim()) return { error: "游標前後都要有文字，才能把前面的字移到上一句" };
  const row = sameRow(project);
  const start = Number(cap.start);
  const prev = (project.captions || [])
    .filter((c) => c.id !== cap.id && !c.title && row(c) === row(cap) && Number(c.start) < start - 1e-3)
    .reduce((a, c) => (!a || Number(c.start) > Number(a.start) ? c : a), null);
  if (!prev) return { error: "這句前面沒有字幕可以接" };
  if (t <= start || t >= Number(cap.end)) return { error: "播放頭不在這句字幕裡，請先把播放頭停在要切開的位置" };
  if (t - start < MIN_LEN || Number(cap.end) - t < MIN_LEN) return { error: "播放頭太靠近這句字幕的開頭或結尾" };
  const at = round(t, 3);
  const body = (prev.text || "").trimEnd();
  const gap = latin.test(body.slice(-1)) && latin.test(words.slice(0, 1)) ? " " : "";
  const marks = headMarks(prev, cap, text, [...words].length, gap);
  const patch = { text: body + gap + words, end: at, ...(marks !== undefined ? { kt: marks } : {}) };
  const lead = text.length - text.trimStart().length;
  const k = [...text.slice(lead, to + after.length - rest.length)].length;
  const own = text === cap.text && fits(cap, text)
    ? { kt: cap.kt.slice(k).map((x) => round(Math.max(0, Number(x) + start - at), 3)) } : {};
  return {
    id: cap.id,
    ops: [{ op: "caption.update", id: prev.id, patch },
      { op: "caption.update", id: cap.id, patch: { text: rest, start: at, ...own } }],
  };
}
