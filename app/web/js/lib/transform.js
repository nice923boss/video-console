// Opacity, move, scale, rotate, trim and distort of a shot's picture (ED-30), the twin of vc/transform.py: the same
// quad and homography, so the preview's CSS matrix3d puts the picture where the export's perspective filter does.
// Positions are fractions of the output frame (x of its width, y of its height) with y up; the panel shows pixels.
export const NEUTRAL = { opacity: 1, x: 0, y: 0, rotation: 0, scale: 1, scaleX: 1, scaleY: 1, anchorX: 0, anchorY: 0 };
export const CORNERS = ["tl", "tr", "br", "bl"];
export const SIDES = ["l", "r", "t", "b"];
export const CROP_MAX = 0.98;

// Every field present, missing ones neutral.
export function fullTransform(t) {
  const d = (t && t.distort) || {};
  return {
    ...NEUTRAL, ...(t || {}),
    crop: { l: 0, r: 0, t: 0, b: 0, ...((t && t.crop) || {}) },
    distort: Object.fromEntries(CORNERS.map((k) => [k, d[k] || [0, 0]])),
  };
}

export function isIdentity(t) {
  if (!t) return true;
  const f = fullTransform(t);
  return Object.keys(NEUTRAL).every((k) => f[k] === NEUTRAL[k])
    && SIDES.every((k) => !f.crop[k]) && CORNERS.every((k) => !f.distort[k][0] && !f.distort[k][1]);
}

// Where the frame's corners land, in frame pixels with y down: [tl, tr, br, bl].
export function frameQuad(t, w, h) {
  const f = fullTransform(t);
  const sx = f.scale * f.scaleX;
  const sy = f.scale * f.scaleY;
  const a = (f.rotation * Math.PI) / 180;
  const cos = Math.cos(a);
  const sin = Math.sin(a);
  const [cx, cy] = [w / 2, h / 2];
  const [ax, ay] = [f.anchorX * w, -f.anchorY * h];
  const [px, py] = [f.x * w, -f.y * h];
  const base = { tl: [0, 0], tr: [w, 0], br: [w, h], bl: [0, h] };
  return CORNERS.map((k) => {
    const vx = (base[k][0] + f.distort[k][0] * w - cx - ax) * sx;
    const vy = (base[k][1] - f.distort[k][1] * h - cy - ay) * sy;
    return [cx + px + vx * cos + vy * sin, cy + py - vx * sin + vy * cos];
  });
}

// [a, b, c, d, e, f, g, h] taking (u, v) of the unit square to the quad: x = (a u + b v + c) / z,
// y = (d u + e v + f) / z, z = g u + h v + 1.
export function homography(quad) {
  const [[x0, y0], [x1, y1], [x2, y2], [x3, y3]] = quad;
  const [dx1, dy1, dx2, dy2] = [x1 - x2, y1 - y2, x3 - x2, y3 - y2];
  const [sx, sy] = [x0 - x1 + x2 - x3, y0 - y1 + y2 - y3];
  const den = dx1 * dy2 - dx2 * dy1;
  let g = 0;
  let h = 0;
  if (!((Math.abs(sx) < 1e-9 && Math.abs(sy) < 1e-9) || Math.abs(den) < 1e-9)) {
    g = (sx * dy2 - dx2 * sy) / den;
    h = (dx1 * sy - sx * dy1) / den;
  }
  return [x1 - x0 + g * x1, x3 - x0 + h * x3, x0, y1 - y0 + g * y1, y3 - y0 + h * y3, y0, g, h];
}

const n = (v) => Number(v.toPrecision(9));

// Styles for a picture element filling a w x h frame drawn cw x ch CSS pixels: {transform, clipPath, opacity},
// with transform-origin 0 0; null when the transform changes nothing.
export function placeCss(t, w, h, cw, ch) {
  if (isIdentity(t) || !cw || !ch) return null;
  const f = fullTransform(t);
  const [a, b, c, d, e, ff, g, hh] = homography(frameQuad(f, w, h).map(([x, y]) => [(x * cw) / w, (y * ch) / h]));
  const m = [a / cw, d / cw, 0, g / cw, b / ch, e / ch, 0, hh / ch, 0, 0, 1, 0, c, ff, 0, 1].map(n);
  const { l, r, t: top, b: bottom } = f.crop;
  const pct = (v) => `${n(v * 100)}%`;
  return {
    transform: `matrix3d(${m.join(",")})`,
    clipPath: l || r || top || bottom ? `inset(${pct(top)} ${pct(r)} ${pct(bottom)} ${pct(l)})` : "",
    opacity: f.opacity < 1 ? String(f.opacity) : "",
  };
}
