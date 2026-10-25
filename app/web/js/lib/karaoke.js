// Karaoke captions (CP-13) in the preview, as vc/karaoke.py burns them in: each character turns the style's
// highlight colour as the caption's voice-over says it. The times are the voice media's charTimes (seconds
// into the file, one per character of the voiced text) moved onto the timeline by the clip's start, trim and speed.
// Characters are counted by code point, as Python counts them.
import { audioSpeed } from "./sequence.js";
import { rgba } from "./caption-style.js";

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
