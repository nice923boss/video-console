// Colour key (GR-09) of a picture-in-picture clip in the preview: an SVG filter after ffmpeg's chromakey, which
// export.py runs. Alpha = clamp((d - similarity) / blend), d the distance of the pixel's U/V from the key colour's,
// sqrt((du² + dv²) / 2) in 0..1 units. ffmpeg averages d over 3x3 pixels and works on subsampled chroma; this
// keys each pixel alone, so edges can differ slightly from the export.

export const DEFAULT_CHROMA = { on: false, color: "#00ff00", similarity: 0.15, blend: 0.1 };

const TABLE = 256;
const fmt = (x) => String(+x.toFixed(5));
const table = (fn) => Array.from({ length: TABLE }, (_, i) => fmt(fn(i / (TABLE - 1)))).join(" ");
const clamp01 = (x) => Math.max(0, Math.min(1, x));

// The key colour's U/V as ffmpeg derives it (full-range BT.601, 0..1).
function keyUV(hex) {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => c / 255);
  return [0.5 + (-0.16874 * r - 0.33126 * g + 0.5 * b), 0.5 + (0.5 * r - 0.41869 * g - 0.08131 * b)];
}

export function chromaMarkup(id, chroma) {
  const c = { ...DEFAULT_CHROMA, ...chroma };
  const sim = Number(c.similarity);
  const blend = Number(c.blend);
  const [ku, kv] = keyUV(c.color);
  // Distances are scaled by reach, a little past where alpha reaches 1, so 8-bit steps stay fine near the edge.
  const reach = Math.min(1, (sim + blend) * 1.1 + 0.01);
  // The pixel's U/V as a limited-range BT.601 video frame (or ffmpeg's conversion of a picture) holds them.
  const s = 224 / 255;
  const o = 128 / 255;
  const uv = [
    [-0.168736 * s, -0.331264 * s, 0.5 * s, 0, o],
    [0.5 * s, -0.418688 * s, -0.081312 * s, 0, o],
    [0, 0, 0, 0, 0],
    [0, 0, 0, 0, 1],
  ].flat().map(fmt).join(" ");
  const half = (k) => table((x) => Math.min(1, (x - k) ** 2 / (2 * reach * reach)));
  // Alpha now holds (d / reach)²; map it to the key's alpha.
  const alpha = table((a) => {
    const d = reach * Math.sqrt(a);
    return blend > 0.0001 ? clamp01((d - sim) / blend) : d > sim ? 1 : 0;
  });
  return `<filter id="${id}" x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB">`
    + `<feColorMatrix in="SourceGraphic" type="matrix" values="${uv}"/>`
    + `<feComponentTransfer><feFuncR type="table" tableValues="${half(ku)}"/><feFuncG type="table" tableValues="${half(kv)}"/></feComponentTransfer>`
    + `<feColorMatrix type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 1 0 0 0"/>`
    + `<feComponentTransfer result="mask"><feFuncA type="table" tableValues="${alpha}"/></feComponentTransfer>`
    + `<feComposite in="SourceGraphic" in2="mask" operator="in"/>`
    + `</filter>`;
}
