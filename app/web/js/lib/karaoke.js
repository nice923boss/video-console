// Karaoke captions (CP-13) in the preview, as vc/karaoke.py burns them in: each character turns the style's
// highlight colour as the caption's voice-over says it. The times are the voice media's charTimes (seconds
// into the file, one per character of the voiced text) moved onto the timeline by the clip's start, trim and speed.
// Characters are counted by code point, as Python counts them.
import { audioSpeed } from "./sequence.js";
import { rgba } from "./caption-style.js";
import { round, clamp } from "./format.js";

// Timeline time each character of the caption's trimmed text lights up, never earlier than the one before;
// punctuation and spaces light with the character before them. null: drawn plainly (no enabled voice clip, no
// times yet, or the caption was edited after it was voiced).
export function voiceStarts(project, cap) {
  const text = (cap.text || "").trim();
  const clip = (project.audio || []).find((a) => a.captionId === cap.id && !a.disabled);
  const media = clip && (project.media || {})[clip.mediaId];
  const times = media && media.charTimes;
  const chars = [...text];
  if (!times || times.length !== chars.length || ((media.source || {}).text || "") !== text) return null;
  const start = Number(clip.start || 0);
  const trim = Number(clip.in || 0);
  const speed = audioSpeed(clip) || 1;
  let last = null;
  const starts = times.map((t) => {
    if (t != null) {
      const at = start + (t - trim) / speed;
      last = last == null ? at : Math.max(last, at);
    }
    return last;
  });
  const first = starts.find((t) => t != null);
  return first == null ? null : starts.map((t) => (t == null ? first : t));
}

// How many leading characters are lit at time t; null when the caption is drawn plainly.
export function litCount(project, cap, t) {
  const starts = voiceStarts(project, cap);
  if (!starts) return null;
  let n = 0;
  while (n < starts.length && starts[n] <= t) n++;
  return n;
}

// Fills a caption's text span: the first `lit` characters in the highlight colour, the rest in the span's own.
export function fillCaption(span, text, lit, s) {
  if (lit == null) {
    span.textContent = text;
    return;
  }
  const chars = [...text];
  const on = document.createElement("span");
  on.textContent = chars.slice(0, lit).join("");
  on.style.color = rgba(s.highlightColor, s.opacity);
  span.replaceChildren(on, chars.slice(lit).join(""));
}

// KTV lyric captions (vc/ktv.py): the caption's own wipe marks, seconds after its start at which each character of
// its trimmed text starts turning the sung colours, then when the last one is done. null when they do not fit the
// text (drawn plainly, or as CP-13 karaoke when it is voiced).
export function ktvMarks(cap) {
  const n = [...(cap.text || "").trim()].length;
  return Array.isArray(cap.kt) && n && cap.kt.length === n + 1 ? cap.kt.map(Number) : null;
}

const KTV_MIN_STEP = 0.01; // as ktv.MIN_STEP: a character takes at least this long

// Fills a KTV caption's text span: each character over a copy of itself in the sung colours (the style's highlight
// colour and highlight outline) that paintKtv uncovers from the left while the character is sung, as the burnt-in
// \clip wipe does. Returns the cells paintKtv updates.
export function fillKtv(span, text, kt, s) {
  const sung = `position:absolute;left:0;top:0;color:${rgba(s.highlightColor, s.opacity)};`
    + `-webkit-text-stroke-color:${s.highlightOutline || s.outlineColor};text-shadow:none;background:none;`
    + "padding:0;margin:0;visibility:hidden";
  const cells = [];
  span.replaceChildren(...[...text].map((ch, i) => {
    if (/\s/.test(ch)) return ch;
    const cell = document.createElement("span");
    cell.style.position = "relative";
    cell.textContent = ch;
    const top = document.createElement("span");
    top.textContent = ch;
    top.style.cssText = sung;
    cell.appendChild(top);
    cells.push({ top, a: kt[i], b: Math.max(kt[i + 1], kt[i] + KTV_MIN_STEP), shown: -1 });
    return cell;
  }));
  return cells;
}

// Uncovers each cell's sung copy for time t, in seconds after the caption's start.
export function paintKtv(cells, t) {
  for (const c of cells) {
    const p = t <= c.a ? 0 : t >= c.b ? 1 : Math.round(((t - c.a) / (c.b - c.a)) * 200) / 200;
    if (p === c.shown) continue;
    c.shown = p;
    c.top.style.visibility = p > 0 ? "visible" : "hidden";
    c.top.style.clipPath = p >= 1 ? "none" : `inset(-0.5em ${((1 - p) * 100).toFixed(1)}% -0.5em -0.5em)`;
  }
}

// Marks after the tick i of kt moved to want (seconds after the caption's start); len is the caption's length.
// Alone the tick stays between its neighbours; with `along` the ticks after it keep their distance from it,
// and the last stays inside the caption unless it already was past its end.
export function dragMarks(kt, i, want, len, along) {
  const n = kt.length - 1;
  const lo = i ? kt[i - 1] : 0;
  if (along) {
    const d = clamp(want - kt[i], lo - kt[i], Math.max(0, len - kt[n]));
    return kt.map((k, j) => (j < i ? k : round(k + d, 3)));
  }
  const hi = i < n ? kt[i + 1] : Math.max(len, kt[n]);
  return kt.map((k, j) => (j === i ? round(clamp(want, lo, hi), 3) : k));
}
