// Blur and mosaic boxes (GR-12) over a shot's picture in the preview, after export.py's _mask_chains: the box,
// blur radius and mosaic block are worked out in frame pixels by the same formulas. The preview blurs with a
// gaussian of the same spread as ffmpeg's two box passes, and its mosaic shows one pixel of each block where
// ffmpeg averages the block, so colours inside the blocks can differ slightly from the export.

export const MAX_MASKS = 8;
export const DEFAULT_MASK = { x: 0.4, y: 0.35, w: 0.2, h: 0.3, mode: "blur", strength: 0.5 };
const BLUR = 0.04; // export.py MASK_BLUR
const BLOCK = 0.06; // export.py MASK_BLOCK

const even = (v) => Math.round(v / 2) * 2;

// The crop export.py cuts for mask m, in frame pixels.
function maskBox(m, width, height) {
  const cw = Math.max(4, even(m.w * width));
  const ch = Math.max(4, even(m.h * height));
  return { cw, ch, cx: Math.min(even(m.x * width), width - cw), cy: Math.min(even(m.y * height), height - ch) };
}

// One pixel near the middle of every (2r + 1)-pixel block, spread over its block.
function pixelate(id, r) {
  const b = 2 * r + 1;
  return `<filter id="${id}" x="0" y="0" width="1" height="1" color-interpolation-filters="sRGB">`
    + `<feFlood x="${r}" y="${r}" width="1" height="1"/><feComposite width="${b}" height="${b}"/><feTile result="grid"/>`
    + `<feComposite in="SourceGraphic" in2="grid" operator="in"/><feMorphology operator="dilate" radius="${r}"/>`
    + `</filter>`;
}

// Markup of the boxes over a slot of `px` CSS pixels per frame pixel; nextId() names each mosaic's SVG filter.
export function maskMarkup(masks, width, height, px, nextId) {
  let defs = "";
  const pct = (v, of) => `${((v / of) * 100).toFixed(3)}%`;
  const boxes = masks.map((m) => {
    const b = maskBox(m, width, height);
    let fx;
    if (m.mode === "mosaic") {
      const block = Math.max(2, Math.round(m.strength * height * BLOCK)) * px;
      const id = nextId();
      defs += pixelate(id, Math.max(1, Math.round((block - 1) / 2)));
      fx = `url(#${id})`;
    } else {
      const r = Math.max(1, Math.min(Math.round(m.strength * height * BLUR), Math.floor(Math.min(b.cw, b.ch) / 4)));
      fx = `blur(${(Math.sqrt((2 * r * r + 2 * r) / 3) * px).toFixed(2)}px)`; // spread of two box blur passes
    }
    return `<div class="pl-mask" style="left:${pct(b.cx, width)};top:${pct(b.cy, height)};width:${pct(b.cw, width)};`
      + `height:${pct(b.ch, height)};backdrop-filter:${fx}"></div>`;
  });
  return (defs ? `<svg class="pl-grade-defs" aria-hidden="true">${defs}</svg>` : "") + boxes.join("");
}
