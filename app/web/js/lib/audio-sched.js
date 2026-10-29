// Sample arithmetic of the preview's sound scheduler (PB-01, components/player-audio.js), kept free of Web Audio so
// it can be tested on its own. A segment is one sound on the timeline played from one file at speed 1 (a sped
// clip's file is its copy at that speed, speedCopy):
// { id, url, s0, f0, f1, fi, fo, track }: timeline frames [f0, f1) play file frames from s0 on, fading in over the
// first fi frames and out over the last fo (0: a hard edge), at 48 kHz.
import { RATE, BLOCK } from "./pcm-cache.js";

export const toFrame = (sec) => Math.round(sec * RATE);

// The name of a speed's audio copy, as audioproxy.speed_key makes it: rounded to 4 decimals, halves up.
export const speedKey = (speed) => String(Math.floor(speed * 10000 + 0.5) / 10000);

// The audio copy of a take or media item at a speed (P2-2): its plain copy at speed 1, else the one made at that
// speed, whose frame k is media time k * speed; undefined until it is made.
export const speedCopy = (item, speed) =>
  speedKey(speed) === "1" ? item.previewAudio : (item.previewAudioSpeeds || {})[speedKey(speed)];

// The runs of timeline frames [from, to) of the segment, each inside one block of the file:
// { F (first timeline frame), n (frames), block, offset (frame in the block) }. Frames before the file's start or
// past its end (frames: the file's length) are silence and get no run.
export function pieces(seg, from, to, frames) {
  const out = [];
  const end = Math.min(to, seg.f1);
  let F = Math.max(from, seg.f0);
  while (F < end) {
    const S = seg.s0 + (F - seg.f0);
    if (S < 0) {
      F = Math.min(end, F - S);
      continue;
    }
    if (S >= frames) break;
    const block = Math.floor(S / BLOCK);
    const offset = S - block * BLOCK;
    const n = Math.min(end - F, BLOCK - offset, frames - S);
    out.push({ F, n, block, offset });
    F += n;
  }
  return out;
}

// The segment's edge gain at timeline frame F: the fades are straight lines, as ffmpeg's afade draws them.
export function edgeValue(seg, F) {
  let v = 1;
  if (seg.fi > 0) v = Math.min(v, (F - seg.f0) / seg.fi);
  if (seg.fo > 0) v = Math.min(v, (seg.f1 - F) / seg.fo);
  return Math.min(1, Math.max(0, v));
}

// [F, value] corners of the edge gain, in order; a straight line runs between neighbours. The fades may overlap on
// a short segment, so where the two lines cross is a corner too.
export function edgePoints(seg) {
  const at = new Set([seg.f0, seg.f1]);
  if (seg.fi > 0) at.add(seg.f0 + seg.fi);
  if (seg.fo > 0) at.add(seg.f1 - seg.fo);
  if (seg.fi > 0 && seg.fo > 0 && seg.fi + seg.fo > seg.f1 - seg.f0) at.add((seg.f0 * seg.fo + seg.f1 * seg.fi) / (seg.fi + seg.fo));
  return [...at].filter((F) => F >= seg.f0 && F <= seg.f1).sort((a, b) => a - b).map((F) => [F, edgeValue(seg, F)]);
}

// What makes a scheduled sound still right: when any of it changes, the sound is stopped and scheduled again.
export const segKey = (seg) => [seg.id, seg.url, seg.s0, seg.f0, seg.f1, seg.fi, seg.fo, seg.track].join("|");

// Timeline frames [F - n, F) of a segment read backwards, under a Hann window and at gain v, into `out` (one array of
// n samples per channel): a grain of J's backwards sound (P2-4). `parts` are its runs (pieces) with `data`, the
// run's block as one array per channel. Grains of n frames a half apart add back up to the sound itself.
export function reverseGrain(out, parts, F, n, v) {
  for (const p of parts) {
    for (let c = 0; c < out.length; c += 1) {
      const src = p.data[c];
      const dst = out[c];
      for (let k = 0; k < p.n; k += 1) {
        const j = F - 1 - p.F - k;
        dst[j] = src[p.offset + k] * v * Math.sin((Math.PI * (j + 0.5)) / n) ** 2;
      }
    }
  }
}

// WSOLA for the faster backwards grains (P2-4): the shift d in [-tol, tol] (in steps of `step`) whose grain ending at
// timeline frame S + d starts most like the last grain carried on, which would end at E: the first len frames of
// each, read backwards, are compared. x holds the sound's frames from timeline frame x0 on.
export function bestShift(x, x0, E, S, len, tol, step) {
  let best = 0;
  let score = -Infinity;
  for (let d = -tol; d <= tol; d += step) {
    let dot = 0;
    let energy = 1e-12;
    for (let i = 0; i < len; i += step) {
      const a = x[S + d - 1 - i - x0];
      dot += a * x[E - 1 - i - x0];
      energy += a * a;
    }
    if (dot / Math.sqrt(energy) > score) {
      score = dot / Math.sqrt(energy);
      best = d;
    }
  }
  return best;
}
