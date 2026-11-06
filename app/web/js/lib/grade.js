// Colour grade of an adjustment layer: the browser half of app/vc/grade.py, which holds the reference
// pixel model (export bakes it into a LUT). The preview draws the same steps as one SVG filter:
//   1. temperature/tint gains and the hue rotation   -> feColorMatrix
//   2. offset, lift, gain and gamma per channel       -> feComponentTransfer tables
//   3. global saturation around Rec.709 luma          -> feColorMatrix
//   4. saturation by tonal range                      -> a luma mask mixed in with feComposite arithmetic
//   5. mix with the untouched picture                 -> feComposite arithmetic
// SVG filters work in 8 bits, so strong range saturation can band a little in the preview, not in the export.
// The white balance block (grade.wb, grade.py's wb_* functions) runs first and the steps above take its output:
//   a. neutralising gains, exposure, temperature/tint, hue and saturation -> one feColorMatrix
//   b. blacks, lift, gain, gamma, fill, brightness, contrast             -> feComponentTransfer tables
//   c. weight by tonal range and mix with the source                     -> luma masks and feComposite arithmetic

export const WHEELS = ["global", "shadows", "midtones", "highlights"];
const NEUTRAL_WHEEL = { r: 0, g: 0, b: 0, sat: 1, bright: 0 };
const NEUTRAL_REST = { temp: 5000, tint: 0, hue: 0, mix: 1 };
// Same fields, order and neutral values as NEUTRAL_WB in grade.py.
export const NEUTRAL_WB = {
  r: 0.5, g: 0.5, b: 0.5, x: 0.5, y: 0.5, size: 0.1, amount: 1,
  exposure: 0, level: 0.5, temp: 0, tint: 0, hue: 0, sat: 0,
  blacks: 0, lift: 0, gain: 0, gamma: 0, fill: 0, bright: 0, contrast: 0,
  shadows: 1, midtones: 1, highlights: 1, mix: 1,
};
const GAIN_LIMITS = [0.25, 4];
const LUMA = [0.2126, 0.7152, 0.0722];
const TABLE = 256; // one entry per 8-bit level, so the tables are exact for the preview's input

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const round4 = (x) => Math.round(x * 1e4) / 1e4;
const fmt = (x) => String(+x.toFixed(5));

export function defaultGrade() {
  return { ...Object.fromEntries(WHEELS.map((w) => [w, { ...NEUTRAL_WHEEL }])), ...NEUTRAL_REST, wb: { ...NEUTRAL_WB } };
}

// Every field present (missing ones neutral), in a fixed key order so two grades compare as JSON.
export function fullGrade(g) {
  const out = {};
  for (const w of WHEELS) out[w] = Object.fromEntries(Object.keys(NEUTRAL_WHEEL).map((k) => [k, g?.[w]?.[k] ?? NEUTRAL_WHEEL[k]]));
  for (const k of Object.keys(NEUTRAL_REST)) out[k] = g?.[k] ?? NEUTRAL_REST[k];
  out.wb = Object.fromEntries(Object.keys(NEUTRAL_WB).map((k) => [k, g?.wb?.[k] ?? NEUTRAL_WB[k]]));
  return out;
}

// The colour wheels and the rest of the main grade change nothing (the white balance block aside).
function mainNeutral(full) {
  if (full.mix <= 0) return true;
  const base = defaultGrade();
  return [...WHEELS, ...Object.keys(NEUTRAL_REST)].every((k) => JSON.stringify(full[k]) === JSON.stringify(base[k]));
}

const lumaOf = (c) => LUMA[0] * c[0] + LUMA[1] * c[1] + LUMA[2] * c[2];

// Channel gains that make the sampled colour grey (amount 1) and, with exposure, bring it to `level`.
export function wbGains(wb) {
  const s = [wb.r, wb.g, wb.b];
  const ls = lumaOf(s);
  const expo = 1 + wb.exposure * (wb.level / Math.max(ls, 0.01) - 1);
  return s.map((c) => Math.max(GAIN_LIMITS[0], Math.min(GAIN_LIMITS[1], (1 + wb.amount * (ls / Math.max(c, 0.01) - 1)) * expo)));
}

const WB_TONE = ["temp", "tint", "hue", "sat", "blacks", "lift", "gain", "gamma", "fill", "bright", "contrast", "shadows", "midtones", "highlights"];

export function wbNeutral(wb) {
  const full = { ...NEUTRAL_WB, ...(wb || {}) };
  if (full.mix <= 0) return true;
  return WB_TONE.every((k) => full[k] === NEUTRAL_WB[k]) && wbGains(full).every((x) => Math.abs(x - 1) < 1e-6);
}

export function isNeutral(g) {
  const full = fullGrade(g);
  return mainNeutral(full) && wbNeutral(full.wb);
}

export function isWheelNeutral(wheel) {
  return Object.keys(NEUTRAL_WHEEL).every((k) => wheel[k] === NEUTRAL_WHEEL[k]);
}

export function neutralWheel() {
  return { ...NEUTRAL_WHEEL };
}

// The puck of a colour wheel from its r/g/b offsets: angle in degrees, red at the top and turning
// clockwise through yellow, green, cyan, blue and magenta; amount 0..1 from the centre to the rim.
export function puckOf(wheel) {
  const x = (wheel.r - (wheel.g + wheel.b) / 2) / 1.5;
  const y = ((Math.sqrt(3) / 2) * (wheel.g - wheel.b)) / 1.5;
  return { angle: (Math.atan2(y, x) * 180) / Math.PI, amount: Math.hypot(x, y) };
}

// The wheel with its puck moved to (angle, amount). The mean of r/g/b (a shift typed into the R/G/B
// fields that lights or darkens all three) is kept.
export function withPuck(wheel, angle, amount) {
  const mean = (wheel.r + wheel.g + wheel.b) / 3;
  const a = (angle * Math.PI) / 180;
  const at = (shift) => round4(Math.max(-1, Math.min(1, mean + amount * Math.cos(a + shift))));
  return { ...wheel, r: at(0), g: at((-2 * Math.PI) / 3), b: at((2 * Math.PI) / 3) };
}

// Temperature and tint as channel gains, followed by the W3C hueRotate matrix (same as grade.py).
function colorMatrix(g) {
  const w = Math.log2(g.temp / 5000);
  const gains = [1 + 0.3 * w, 1 - (0.3 * g.tint) / 100, 1 - 0.3 * w];
  const a = (g.hue * Math.PI) / 180;
  const c = Math.cos(a);
  const s = Math.sin(a);
  const hue = [
    [0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928],
    [0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.14, 0.072 - c * 0.072 - s * 0.283],
    [0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072],
  ];
  return hue.map((row) => row.map((v, j) => v * gains[j]));
}

function channelCurve(g, ch) {
  const { shadows: sh, midtones: mid, highlights: hi, global: gl } = g;
  const lift = 0.25 * (sh[ch] + sh.bright);
  const gain = 1 + 0.5 * (hi[ch] + hi.bright);
  const power = 2 ** -(mid[ch] + mid.bright);
  const off = 0.3 * (gl[ch] + gl.bright);
  return (x) => clamp01(clamp01(lift + clamp01(x + off) * (gain - lift)) ** power);
}

function rangeSaturation(g, L) {
  return (1 - L) ** 2 * g.shadows.sat + 2 * L * (1 - L) * g.midtones.sat + L ** 2 * g.highlights.sat;
}

function wbMatrix(wb) {
  const n = wbGains(wb);
  const w = wb.temp / 100;
  const k = wb.tint / 100;
  const gains = [n[0] * (1 + 0.3 * w), n[1] * (1 - 0.3 * k), n[2] * (1 - 0.3 * w)];
  const hue = colorMatrix({ temp: 5000, tint: 0, hue: wb.hue });
  const s = 1 + wb.sat;
  const sat = [0, 1, 2].map((i) => LUMA.map((l, j) => (i === j ? s : 0) + (1 - s) * l));
  const sh = [0, 1, 2].map((i) => [0, 1, 2].map((j) => sat[i][0] * hue[0][j] + sat[i][1] * hue[1][j] + sat[i][2] * hue[2][j]));
  return sh.map((row) => row.map((v, j) => v * gains[j]));
}

function wbCurve(wb) {
  const blk = wb.blacks * 0.1;
  const lift = wb.lift * 0.25;
  const gain = 1 + 0.5 * wb.gain;
  const power = 2 ** -wb.gamma;
  const fill = 1.2 * wb.fill;
  const bright = 0.25 * wb.bright;
  const contrast = 2 ** wb.contrast;
  return (x0) => {
    let x = clamp01((x0 - blk) / (1 - blk));
    x = clamp01(x + lift * (1 - x));
    x = clamp01(x * gain);
    x = clamp01(x ** power);
    x = clamp01(x + fill * x * (1 - x) ** 2);
    x = clamp01(x + bright);
    return clamp01(0.5 + (x - 0.5) * contrast);
  };
}

const wbWeight = (wb, L) => wb.mix * ((1 - L) ** 2 * wb.shadows + 2 * L * (1 - L) * wb.midtones + L ** 2 * wb.highlights);

// The white balance steps as filter primitives reading SourceGraphic; returns [primitives, result name].
function wbParts(wb) {
  const parts = [`<feColorMatrix in="SourceGraphic" type="matrix" values="${matrixValues(wbMatrix(wb))}" result="wm"/>`];
  const tone = ["blacks", "lift", "gain", "gamma", "fill", "bright", "contrast"].some((k) => wb[k] !== 0);
  let last = "wm";
  if (tone) {
    const t = table(wbCurve(wb));
    parts.push(transfer("wm", "wt", [t, t, t]));
    last = "wt";
  }
  if (["shadows", "midtones", "highlights"].some((k) => wb[k] !== 1)) {
    // out = W*t + (1 - W)*src with W = mix * w(L of the source), both masks read from one luma image.
    const luma = [LUMA, LUMA, LUMA];
    const on = table((L) => wbWeight(wb, L));
    const off = table((L) => 1 - wbWeight(wb, L));
    parts.push(`<feColorMatrix in="SourceGraphic" type="matrix" values="${matrixValues(luma, [0, 0, 0], "0 0 0 0 1")}" result="wl"/>`);
    parts.push(transfer("wl", "wk", [on, on, on]));
    parts.push(transfer("wl", "wj", [off, off, off]));
    parts.push(`<feComposite in="${last}" in2="wk" operator="arithmetic" k1="1" k2="0" k3="0" k4="0" result="wa"/>`);
    parts.push(`<feComposite in="SourceGraphic" in2="wj" operator="arithmetic" k1="1" k2="0" k3="0" k4="0" result="wo"/>`);
    parts.push(`<feComposite in="wa" in2="wo" operator="arithmetic" k1="0" k2="1" k3="1" k4="0" result="wb"/>`);
    return [parts, "wb"];
  }
  if (wb.mix < 1) {
    parts.push(`<feComposite in="${last}" in2="SourceGraphic" operator="arithmetic" k1="0" k2="${fmt(wb.mix)}" k3="${fmt(1 - wb.mix)}" k4="0" result="wb"/>`);
    return [parts, "wb"];
  }
  return [parts, last];
}

const table = (fn) => Array.from({ length: TABLE }, (_, i) => fmt(fn(i / (TABLE - 1)))).join(" ");

// feColorMatrix values from a 3x3 matrix plus per-channel offsets; alpha is kept, or forced to 1 for the
// helper images that feComposite arithmetic combines (it works on premultiplied colour).
function matrixValues(m, offset = [0, 0, 0], alpha = "0 0 0 1 0") {
  return `${m.map((row, i) => `${row.map(fmt).join(" ")} 0 ${fmt(offset[i])}`).join(" ")} ${alpha}`;
}

const transfer = (input, result, fns) =>
  `<feComponentTransfer in="${input}" result="${result}">${["R", "G", "B"]
    .map((ch, i) => `<feFunc${ch} type="table" tableValues="${fns[i]}"/>`).join("")}</feComponentTransfer>`;

// The grade as an SVG <filter> element (markup), referenced from CSS as url(#id).
// split (GR-06): grade only the right half of the element, leaving its left half as it was, for a
// before/after comparison.
export function filterMarkup(id, grade, split = false) {
  const g = fullGrade(grade);
  const [parts, src] = wbNeutral(g.wb) ? [[], "SourceGraphic"] : wbParts(g.wb);
  let last = src;
  if (!mainNeutral(g)) last = mainParts(g, src, parts);
  return finish(id, parts, last, split);
}

// The main grade's primitives appended to parts, reading `src`; returns the last result name.
function mainParts(g, src, parts) {
  parts.push(`<feColorMatrix in="${src}" type="matrix" values="${matrixValues(colorMatrix(g))}" result="m"/>`);
  parts.push(transfer("m", "c", ["r", "g", "b"].map((ch) => table(channelCurve(g, ch)))));
  let last = "c";
  const gs = g.global.sat;
  if (gs !== 1) {
    const sat = [0, 1, 2].map((i) => LUMA.map((l, j) => (i === j ? gs : 0) + (1 - gs) * l));
    parts.push(`<feColorMatrix in="c" type="matrix" values="${matrixValues(sat)}" result="s"/>`);
    last = "s";
  }
  if (["shadows", "midtones", "highlights"].some((w) => g[w].sat !== 1)) {
    // out = c + k(L)(c - L) with k = s(L) - 1 in -1..1. Filter images cannot hold negatives, so the
    // terms travel halved around 0.5: d = (c - L)/2 + 0.5, mask = s(L)/2, and
    // e = 2*d*mask - d - mask + 1 = k(c - L)/2 + 0.5, giving out = 2e + c - 1.
    const diff = [0, 1, 2].map((i) => LUMA.map((l, j) => 0.5 * ((i === j ? 1 : 0) - l)));
    const luma = [LUMA, LUMA, LUMA];
    const mask = table((L) => rangeSaturation(g, L) / 2);
    parts.push(`<feColorMatrix in="${last}" type="matrix" values="${matrixValues(diff, [0.5, 0.5, 0.5], "0 0 0 0 1")}" result="d"/>`);
    parts.push(`<feColorMatrix in="${last}" type="matrix" values="${matrixValues(luma, [0, 0, 0], "0 0 0 0 1")}" result="l"/>`);
    parts.push(transfer("l", "k", [mask, mask, mask]));
    parts.push(`<feComposite in="d" in2="k" operator="arithmetic" k1="2" k2="-1" k3="-1" k4="1" result="e"/>`);
    parts.push(`<feComposite in="e" in2="${last}" operator="arithmetic" k1="0" k2="2" k3="1" k4="-1" result="r"/>`);
    last = "r";
  }
  if (g.mix < 1) {
    parts.push(`<feComposite in="${last}" in2="${src}" operator="arithmetic" k1="0" k2="${fmt(g.mix)}" k3="${fmt(1 - g.mix)}" k4="0" result="x"/>`);
    last = "x";
  }
  return last;
}

function finish(id, parts, last, split) {
  // Keep the picture's own transparency (letterbox areas, picture-in-picture edges).
  if (!split) {
    parts.push(`<feComposite in="${last}" in2="SourceGraphic" operator="in"/>`);
    return `<filter id="${id}" x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB">${parts.join("")}</filter>`;
  }
  // Primitive subregions in bounding-box units: the graded result on the right half, the source on the left.
  parts.push(`<feComposite in="${last}" in2="SourceGraphic" operator="in" x="0.5" width="0.5" result="after"/>`);
  parts.push(`<feOffset in="SourceGraphic" x="0" width="0.5" result="before"/>`);
  parts.push(`<feMerge><feMergeNode in="before"/><feMergeNode in="after"/></feMerge>`);
  return `<filter id="${id}" x="0" y="0" width="1" height="1" primitiveUnits="objectBoundingBox" color-interpolation-filters="sRGB">${parts.join("")}</filter>`;
}
