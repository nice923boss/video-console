// Video scopes (GR-07) worked out from RGBA pixels (canvas ImageData): a luma waveform and a vectorscope, with
// Rec.709 luma and colour difference, the colour space the export writes (QA-04).
const KR = 0.2126;
const KB = 0.0722;
const KG = 1 - KR - KB;

export const LEVELS = 256;

// counts[row * cols + col]: how many pixels of each output column have each luma level, row 0 white (255) down
// to row 255 black, the way the scope draws it.
export function waveform(data, w, h, cols) {
  const counts = new Uint32Array(cols * LEVELS);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const level = Math.round(KR * data[i] + KG * data[i + 1] + KB * data[i + 2]);
      counts[(LEVELS - 1 - level) * cols + Math.floor((x * cols) / w)]++;
    }
  }
  return counts;
}

// Where an RGB colour (0..1 each) lands on the vectorscope: x is Cb, y is Cr, both -0.5..0.5 (red up and left).
export function chroma(r, g, b) {
  const y = KR * r + KG * g + KB * b;
  return [(b - y) / (2 * (1 - KB)), (r - y) / (2 * (1 - KR))];
}

// counts[row * size + col] over a size x size grid, Cr = 0.5 on the top row and Cb = -0.5 on the left column.
export function vectorscope(data, size) {
  const counts = new Uint32Array(size * size);
  const s = size - 1;
  for (let i = 0; i < data.length; i += 4) {
    const [cb, cr] = chroma(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255);
    counts[Math.round((0.5 - cr) * s) * size + Math.round((0.5 + cb) * s)]++;
  }
  return counts;
}

// The 75% colour bars every vectorscope marks, as [label, cb, cr].
export const TARGETS = [
  ["紅", 0.75, 0, 0], ["洋紅", 0.75, 0, 0.75], ["藍", 0, 0, 0.75],
  ["青", 0, 0.75, 0.75], ["綠", 0, 0.75, 0], ["黃", 0.75, 0.75, 0],
].map(([label, r, g, b]) => [label, ...chroma(r, g, b)]);

// The skin tone line: most skin, whatever its shade, falls near this angle (degrees from +Cb toward +Cr).
export const SKIN_ANGLE = 123;

// RGBA pixels for counts: each cell lit by how many pixels fell in it, full at `full` pixels. colors is one
// [r, g, b] for every cell or a list with each cell's own.
export function paint(counts, full, colors) {
  const out = new Uint8ClampedArray(counts.length * 4);
  const each = Array.isArray(colors[0]);
  for (let k = 0; k < counts.length; k++) {
    if (!counts[k]) continue;
    const [r, g, b] = each ? colors[k] : colors;
    const j = k * 4;
    out[j] = r;
    out[j + 1] = g;
    out[j + 2] = b;
    out[j + 3] = Math.round(255 * Math.min(1, 0.25 + 0.75 * Math.sqrt(counts[k] / full)));
  }
  return out;
}

// The colour of each vectorscope cell: the hue it stands for at a middle brightness, so the trace shows colours.
export function vectorColors(size) {
  const s = size - 1;
  const out = new Array(size * size);
  for (let row = 0; row < size; row++) {
    for (let col = 0; col < size; col++) {
      const cb = col / s - 0.5;
      const cr = 0.5 - row / s;
      const y = 0.6;
      const r = y + 2 * (1 - KR) * cr;
      const b = y + 2 * (1 - KB) * cb;
      const g = (y - KR * r - KB * b) / KG;
      out[row * size + col] = [r, g, b].map((v) => Math.round(255 * Math.min(1, Math.max(0.15, v))));
    }
  }
  return out;
}
