// Mirror of app/vc/keyframes.py: a clip's keyframes [{t, <field>: value}] (t is seconds into the clip) move a
// field in straight lines between them and hold before the first and after the last. Grades blend number by
// number; the export bakes a grade into at most MAX_STEPS steps per change, and the preview shows the same steps.

const MAX_STEPS = 30;

export const points = (item, field) => (item.keyframes || []).filter((k) => field in k).map((k) => [Number(k.t), k[field]]);

export const hasKeys = (item, field) => (item.keyframes || []).some((k) => field in k);

export function lerp(a, b, f) {
  if (a && typeof a === "object") return Object.fromEntries(Object.keys(a).map((k) => [k, lerp(a[k], b[k], f)]));
  return a + (b - a) * f;
}

export function valueAt(item, field, t, fallback) {
  const pts = points(item, field);
  if (!pts.length) return item[field] ?? fallback;
  if (t <= pts[0][0]) return pts[0][1];
  for (let i = 0; i + 1 < pts.length; i += 1) {
    const [t0, v0] = pts[i];
    const [t1, v1] = pts[i + 1];
    if (t < t1) return lerp(v0, v1, (t - t0) / (t1 - t0));
  }
  return pts[pts.length - 1][1];
}

// The value of the export step (steps() in keyframes.py) that holds t seconds into the clip.
export function steppedAt(item, field, t, length, fps) {
  const pts = points(item, field);
  if (!pts.length) return item[field];
  if (t < pts[0][0]) return pts[0][1];
  for (let i = 0; i + 1 < pts.length; i += 1) {
    const [t0, v0] = pts[i];
    const [t1, v1] = pts[i + 1];
    const a = Math.max(t0, 0);
    const b = Math.min(t1, length);
    if (t >= t1 || b <= a) continue;
    if (JSON.stringify(v0) === JSON.stringify(v1)) return v0;
    const n = Math.max(1, Math.min(MAX_STEPS, Math.ceil((b - a) * fps)));
    const k = Math.min(n - 1, Math.floor(((t - a) / (b - a)) * n));
    const mid = (a + ((b - a) * (k + 0.5)) / n - t0) / (t1 - t0);
    return n > 1 ? lerp(v0, v1, mid) : v0;
  }
  return pts[pts.length - 1][1];
}

// Keyframes with one more key at t (replacing a key already at t for that field set).
export function withKey(item, t, values) {
  const at = Math.round(t * 1000) / 1000;
  const keys = (item.keyframes || []).filter((k) => Math.abs(k.t - at) > 1e-3);
  const old = (item.keyframes || []).find((k) => Math.abs(k.t - at) <= 1e-3) || {};
  return [...keys, { ...old, ...values, t: at }].sort((a, b) => a.t - b.t);
}

const r3 = (v) => Math.round(v * 1000) / 1000;

// Keyframes of the two halves of a clip cut d seconds in. Each half gets a key at the cut that holds what
// was playing there, so neither half changes how it looks or sounds.
export function splitKeys(item, d) {
  const keys = item.keyframes || [];
  const fields = [...new Set(keys.flatMap((k) => Object.keys(k).filter((f) => f !== "t")))];
  const edge = Object.fromEntries(fields.map((f) => [f, valueAt(item, f, d)]));
  const head = [...keys.filter((k) => k.t < d - 1e-3), { ...edge, t: r3(d) }];
  const tail = [{ ...edge, t: 0 }, ...keys.filter((k) => k.t > d + 1e-3).map((k) => ({ ...k, t: r3(k.t - d) }))];
  return [head, tail];
}

// Keyframes once a clip's start moves d seconds later (d < 0: earlier) while its content stays put on the
// timeline, as when its left edge is trimmed.
export function shiftKeys(item, d) {
  const keys = item.keyframes || [];
  if (!keys.length || !d) return keys;
  if (d < 0) return keys.map((k) => ({ ...k, t: r3(k.t - d) }));
  return splitKeys(item, d)[1];
}

// {keyframes} for an ops patch when the item has keyframes, else nothing.
export const keyPatch = (item, keys) => ((item.keyframes || []).length ? { keyframes: keys } : {});

// Mirror of take_keys(): a version's 壓低爆音 keys times its hand-set keys (t in file seconds), as one list.
export function takeKeys(take) {
  const a = take.volumeKeys || [];
  const b = take.gainKeys || [];
  if (!a.length || !b.length) return a.length ? a : b;
  const times = [...new Set([...a, ...b].map((k) => r3(Number(k.t))))].sort((x, y) => x - y);
  return times.map((t) => ({ t, volume: Math.round(valueAt({ keyframes: a }, "volume", t, 1)
    * valueAt({ keyframes: b }, "volume", t, 1) * 1e4) / 1e4 }));
}
