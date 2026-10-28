// Waveforms drawn on the timeline's audio clips and on the picture clips' own sound, shaped by the
// clip's volume, keyframes and fades so the lane shows what will be heard.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { fileUrl } from "../lib/format.js";
import { valueAt } from "../lib/keyframes.js";
import { declip } from "../lib/declip.js";
import { AUDIO_H } from "./tl-tracks.js";

const WAVE_MAX_PX = 8000;
const waveCache = new Map();

function loadWave(url) {
  if (!waveCache.has(url)) {
    const forget = () => {
      waveCache.delete(url);
      return null;
    };
    waveCache.set(url, fetch(url).then((r) => (r.ok ? r.json() : forget())).catch(forget));
  }
  return waveCache.get(url);
}

// Gain a clip plays its sound at t seconds in: its volume (keyframes included) and fades, as
// player-engine.js drawAudio applies them. A picture clip's own sound passes its shot: only a volume.
export function playGain(item, t, len) {
  if (!item) return 1;
  const fi = Math.min(Number(item.fadeIn || 0), len / 2);
  const fo = Math.min(Number(item.fadeOut || 0), len / 2);
  let gain = Number(valueAt(item, "volume", t, 1));
  if (fi > 0 && t < fi) gain *= t / fi;
  if (fo > 0 && t > len - fo) gain *= (len - t) / fo;
  return gain;
}

// Bars follow the clip's gain. A wave that knows its file's level (media.py waveform) is drawn against
// full scale, red where the gain pushes it past; older waves stay drawn against their own loudest peak.
// len is timeline seconds; a sped clip (speed source seconds per timeline second) covers len * speed of the file.
// keys: a version's volume keys (t in file seconds) on top of a picture clip's own sound. Each column is the
// loudest of its buckets, each at the gain inside both, so a dip from declip.js clears the red at any zoom.
function drawWave(canvas, data, tIn, len, width, item, speed, keys) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.min(WAVE_MAX_PX, Math.round(width * dpr)));
  const h = Math.max(1, Math.round((canvas.clientHeight || AUDIO_H - 16) * dpr));
  canvas.width = w;
  canvas.height = h;
  const g = canvas.getContext("2d");
  const peaks = data && Array.isArray(data.peaks) ? data.peaks : null;
  const dur = data ? Number(data.duration) : 0;
  if (!g || !peaks || !peaks.length || !(dur > 0)) return;
  const style = getComputedStyle(canvas);
  const color = style.color;
  const over = style.getPropertyValue("--danger").trim() || color;
  const level = Number(data.level) > 0 ? Number(data.level) : 0;
  const n = peaks.length;
  const keyed = keys && keys.length ? { keyframes: keys } : null;
  const gain = (t) => playGain(item, t, len) * (keyed ? Number(valueAt(keyed, "volume", tIn + t * speed, 1)) : 1);
  for (let x = 0; x < w; x += 1) {
    const a = (x / w) * len;
    const b = ((x + 1) / w) * len;
    const i0 = Math.max(0, Math.floor(((tIn + a * speed) / dur) * n));
    const i1 = Math.min(n, Math.max(i0 + 1, Math.ceil(((tIn + b * speed) / dur) * n)));
    let amp = 0;
    for (let i = i0; i < i1; i += 1) {
      const t = Math.min(b, Math.max(a, (((i + 0.5) / n) * dur - tIn) / speed));
      amp = Math.max(amp, (peaks[i] / 100) * (level || 1) * gain(t));
    }
    const bar = Math.max(1, Math.min(1, amp) * h * 0.9);
    g.fillStyle = level && amp > 1 ? over : color;
    g.fillRect(x, (h - bar) / 2, 1, bar);
  }
}

// The 壓低爆音 dips (declip.js) for a clip's sound, from the wave its lane draws; null when that wave has no level.
export async function findLoud(pid, media, tIn, len, speed, gainAt, room) {
  const data = media && media.wave ? await loadWave(fileUrl(pid, media.wave)) : null;
  return declip(data, tIn, len, speed, gainAt, room);
}

export function Wave({ pid, media, tIn, len, width, item, speed = 1, keys = null }) {
  const ref = useRef(null);
  const [data, setData] = useState(null);
  const url = media && media.wave ? fileUrl(pid, media.wave) : "";
  useEffect(() => {
    let alive = true;
    setData(null);
    if (url) loadWave(url).then((d) => alive && setData(d));
    return () => {
      alive = false;
    };
  }, [url]);
  useEffect(() => {
    if (ref.current) drawWave(ref.current, data, tIn, len, width, item, speed, keys);
  }, [data, tIn, len, width, item, speed, keys]);
  return url ? html`<canvas class="tl-wave" ref=${ref}></canvas>` : null;
}
