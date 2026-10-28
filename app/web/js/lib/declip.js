// The 壓低爆音 button: finds where a clip's sound passes full scale (drawn red on its timeline wave, tl-wave.js)
// and the volume dips that bring each part back under. Times are seconds into the clip.
import { valueAt } from "./keyframes.js";

const TARGET = 0.97; // loudest peak a dip leaves, a little under full scale
const RAMP = 0.1; // seconds a dip takes to go down and to come back (as edits.py volume)
const PAD = 0.01; // at least this much quiet on each side of a loud bucket, so rounding key times keeps it covered
const MAX_KEYS = 200; // opfields.py MAX_KEYFRAMES

// [{s, e, gain}]: one per bucket over full scale, padded by a bucket, with the gain that brings it to TARGET.
// wave: media.py waveform {peaks 0..100 of the file's loudest, duration, level}; the clip plays file seconds
// tIn + t * speed for t in 0..len, at gainAt(t). Sampled as tl-wave.js draws it: each bucket at its middle.
function loudBuckets(wave, tIn, len, speed, gainAt) {
  const { peaks } = wave;
  const n = peaks.length;
  const dur = Number(wave.duration);
  const level = Number(wave.level);
  const step = dur / n / speed;
  const pad = Math.max(step, PAD);
  const first = Math.max(0, Math.floor((tIn / dur) * n));
  const last = Math.min(n, Math.ceil(((tIn + len * speed) / dur) * n));
  const out = [];
  for (let i = first; i < last; i += 1) {
    const mid = (((i + 0.5) / n) * dur - tIn) / speed;
    const amp = (peaks[i] / 100) * level * gainAt(Math.min(len, Math.max(0, mid)));
    if (amp > 1) out.push({ s: Math.max(0, mid - step / 2 - pad), e: Math.min(len, mid + step / 2 + pad), gain: TARGET / amp });
  }
  return out;
}

// Spans closer than gap become one, at the lower gain.
function merge(spans, gap) {
  return spans.reduce((acc, x) => {
    const prev = acc[acc.length - 1];
    return prev && x.s - prev.e < gap
      ? [...acc.slice(0, -1), { s: prev.s, e: Math.max(prev.e, x.e), gain: Math.min(prev.gain, x.gain) }]
      : [...acc, x];
  }, []);
}

// Volume multiplier keys: down over RAMP before each span, flat across it, back up over RAMP after.
function dipKeys(spans, len) {
  const r3 = (v) => Math.round(v * 1000) / 1000;
  return spans.flatMap(({ s, e, gain }) => {
    const g = Math.floor(gain * 10000) / 10000;
    return [
      ...(s - RAMP > 0 ? [{ t: r3(s - RAMP), volume: 1 }] : []),
      { t: r3(s), volume: g },
      { t: r3(e), volume: g },
      ...(e + RAMP < len ? [{ t: r3(e + RAMP), volume: 1 }] : []),
    ];
  });
}

// {spans, keys}: the loud parts and multiplier keys (at most room of them) that bring them under full scale.
// No level in the wave (made before media.py measured it): null, as the red is not drawn either.
export function declip(wave, tIn, len, speed, gainAt, room = MAX_KEYS) {
  if (!wave || !Array.isArray(wave.peaks) || !wave.peaks.length || !(Number(wave.level) > 0) || !(Number(wave.duration) > 0)) {
    return null;
  }
  let gap = 2 * RAMP;
  let spans = merge(loudBuckets(wave, tIn, len, speed || 1, gainAt), gap);
  while (spans.length * 4 > room && spans.length > 1) {
    gap *= 2;
    spans = merge(spans, gap);
  }
  return { spans, keys: dipKeys(spans, len) };
}

// An audio clip's volume keyframes with the dips multiplied in: a key wherever either had one.
export function withDips(item, dips, len) {
  const times = [...(item.keyframes || []).filter((k) => "volume" in k).map((k) => k.t), ...dips.map((k) => k.t)]
    .filter((t) => t >= 0 && t <= len);
  const dip = { keyframes: dips };
  return [...new Set(times)].sort((a, b) => a - b).map((t) => ({
    t,
    volume: Math.min(4, Math.round(Number(valueAt(item, "volume", t, 1)) * Number(valueAt(dip, "volume", t, 1)) * 10000) / 10000),
  }));
}
