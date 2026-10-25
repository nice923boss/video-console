// Colour grade of an adjustment layer: the browser half of app/vc/grade.py, which holds the reference
// pixel model (export bakes it into a LUT). The preview draws the same steps as one SVG filter:
//   1. temperature/tint gains and the hue rotation   -> feColorMatrix
//   2. offset, lift, gain and gamma per channel       -> feComponentTransfer tables
//   3. global saturation around Rec.709 luma          -> feColorMatrix
//   4. saturation by tonal range                      -> a luma mask mixed in with feComposite arithmetic
//   5. mix with the untouched picture                 -> feComposite arithmetic
// SVG filters work in 8 bits, so strong range saturation can band a little in the preview, not in the export.

export const WHEELS = ["global", "shadows", "midtones", "highlights"];
const NEUTRAL_WHEEL = { r: 0, g: 0, b: 0, sat: 1, bright: 0 };
const NEUTRAL_REST = { temp: 5000, tint: 0, hue: 0, mix: 1 };
const LUMA = [0.2126, 0.7152, 0.0722];
const TABLE = 256; // one entry per 8-bit level, so the tables are exact for the preview's input

const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const round4 = (x) => Math.round(x * 1e4) / 1e4;
const fmt = (x) => String(+x.toFixed(5));

export function defaultGrade() {
  return { ...Object.fromEntries(WHEELS.map((w) => [w, { ...NEUTRAL_WHEEL }])), ...NEUTRAL_REST };
}

// Every field present (missing ones neutral), in a fixed key order so two grades compare as JSON.
export function fullGrade(g) {
  const out = {};
  for (const w of WHEELS) out[w] = Object.fromEntries(Object.keys(NEUTRAL_WHEEL).map((k) => [k, g?.[w]?.[k] ?? NEUTRAL_WHEEL[k]]));
  for (const k of Object.keys(NEUTRAL_REST)) out[k] = g?.[k] ?? NEUTRAL_REST[k];
  return out;
}

export function isNeutral(g) {
  const full = fullGrade(g);
  return full.mix <= 0 || JSON.stringify(full) === JSON.stringify(defaultGrade());
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
  const parts = [`<feColorMatrix in="SourceGraphic" type="matrix" values="${matrixValues(colorMatrix(g))}" result="m"/>`];
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
    parts.push(`<feComposite in="${last}" in2="SourceGraphic" operator="arithmetic" k1="0" k2="${fmt(g.mix)}" k3="${fmt(1 - g.mix)}" k4="0" result="x"/>`);
    last = "x";
  }
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
