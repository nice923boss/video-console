// Caption styles in the browser. resolveStyle and anchorOf follow vc/captions.py, and the CSS builders mirror
// its ASS layers (box, shadow, text with outline), so the preview places and draws each caption the way the
// burnt-in export does. Sizes are pixels on a 1080-high frame; in the preview 1px is 1/10.8 cqh of the stage.
// The ops builders return one list per user action, so every change is a single undo step.
import { setState } from "./store.js";
import { newId, round } from "./format.js";

// Mirrors DEFAULT_CAPTION_STYLE in vc/ops.py: the look captions had before styles existed.
export const DEFAULT_STYLE = {
  name: "預設", font: "Microsoft JhengHei", size: 52, color: "#ffffff", opacity: 1, bold: true,
  italic: false, spacing: 0, outline: true, outlineColor: "#000000", outlineWidth: 4,
  shadow: true, shadowColor: "#000000", shadowOpacity: 0.4, shadowDistance: 1, shadowBlur: 0,
  box: false, boxColor: "#000000", boxOpacity: 0.6, boxPadding: 12, align: "center",
  marginV: 65, marginH: 80, x: null, y: null,
};
export const MAX_STYLES = 50;

// The export's caption size from before styles; still the size of the built-in look.
export const legacySize = (settings) => (settings && settings.export && settings.export.captionSize) || 52;

export const styleById = (project, id) => (id ? (project.captionStyles || []).find((s) => s.id === id) || null : null);
export const defaultStyleId = (project) => {
  const id = project.settings && project.settings.captionStyle;
  return styleById(project, id) ? id : null;
};

// The style a caption draws with: its own, else the project default, else the built-in look (id null).
export function resolveStyle(project, cap, legacy = 52) {
  const style = styleById(project, cap && cap.style) || styleById(project, defaultStyleId(project));
  if (!style) return { ...DEFAULT_STYLE, id: null, size: legacy };
  return { ...DEFAULT_STYLE, ...style, font: style.font || DEFAULT_STYLE.font };
}

// Where a dragged caption sits as fractions of the frame; null when it follows its top/middle/bottom row.
export function anchorOf(style, cap) {
  for (const item of [cap, style]) {
    if (item && item.x != null && item.y != null) return { x: item.x, y: item.y };
  }
  return null;
}

export const usesStyle = (project, cap, id, legacy) => resolveStyle(project, cap, legacy).id === id;

// ---------------------------------------------------------------- preview CSS
const u = (px) => `${round(Number(px) / 10.8, 3)}cqh`;

function rgba(hex, alpha = 1) {
  const n = parseInt(String(hex).slice(1), 16) || 0;
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${round(alpha, 3)})`;
}

function shadowCss(s) {
  const colour = rgba(s.shadowColor, s.shadowOpacity);
  // CSS blur radius is about twice the gaussian sigma libass uses for \blur.
  if (s.shadowDistance > 0) return `${u(s.shadowDistance)} ${u(s.shadowDistance)} ${u(s.shadowBlur * 2)} ${colour}`;
  return `0 0 ${u(Math.max(s.shadowBlur * 2, 1))} ${colour}`; // distance 0: a glow
}

// libass sizes a font by its ascent + descent, CSS by its em; for Microsoft JhengHei the em is 0.75 of that.
// This is the em per ASS size unit, measured once per font stack.
const emScales = new Map();
function emScale(family) {
  if (!emScales.has(family)) {
    const ctx = document.createElement("canvas").getContext("2d");
    ctx.font = `1000px ${family}`;
    const m = ctx.measureText("字");
    const height = m.fontBoundingBoxAscent + m.fontBoundingBoxDescent;
    emScales.set(family, height > 0 ? 1000 / height : 1);
  }
  return emScales.get(family);
}

// The inline text span: font, fill, outline, shadow and the box behind each line.
export function textCss(s) {
  const font = String(s.font || DEFAULT_STYLE.font).replace(/["\\;{}<>]/g, "");
  const family = `"${font}","${DEFAULT_STYLE.font}",sans-serif`;
  const parts = [
    `font-family:${family}`,
    // Lines are one ASS size apart, as libass spaces them.
    `font-size:${u(s.size * emScale(family))}`,
    `line-height:${u(s.size)}`,
    `font-weight:${s.bold ? 700 : 400}`,
    `font-style:${s.italic ? "italic" : "normal"}`,
    `letter-spacing:${u(s.spacing)}`,
    `color:${rgba(s.color, s.opacity)}`,
  ];
  // ASS draws the outline outside the glyph; a centred stroke twice as wide, painted under the fill, matches it.
  if (s.outline && s.outlineWidth > 0) {
    parts.push(`-webkit-text-stroke:${u(s.outlineWidth * 2)} ${s.outlineColor}`, "paint-order:stroke fill");
  }
  if (s.shadow) parts.push(`text-shadow:${shadowCss(s)}`);
  if (s.box) {
    // The box grows around the text without moving it, like libass's opaque box.
    parts.push(`background:${rgba(s.boxColor, s.boxOpacity)}`, `padding:${u(s.boxPadding)}`, `margin:0 -${u(s.boxPadding)}`);
  }
  return parts.join(";");
}

const ALIGN_SELF = { left: "flex-start", center: "center", right: "flex-end" };
const SHIFT_X = { left: "0%", center: "-50%", right: "-100%" };

// The caption block: in its row (inside a zone) or pinned at a dragged anchor.
export function blockCss(s, point) {
  const common = `text-align:${s.align};max-width:calc(100cqw - ${u(s.marginH * 2)})`;
  if (!point) return `${common};align-self:${ALIGN_SELF[s.align]};margin:0 ${u(s.marginH)}`;
  return `${common};position:absolute;width:max-content;left:${round(point.x * 100, 3)}%;top:${round(point.y * 100, 3)}%;`
    + `transform:translate(${SHIFT_X[s.align]},-50%)`;
}

// A row's distance from the frame edge (the middle row ignores it, as in ASS).
export const zonePad = (row, styles) => {
  const v = Math.max(0, ...styles.map((s) => s.marginV));
  return row === "top" ? `padding-top:${u(v)}` : row === "bottom" ? `padding-bottom:${u(v)}` : "";
};

// ---------------------------------------------------------------- dragging scope (per viewer)
const SCOPE_KEY = "vc.captions.dragScope";

export function dragScope(state) {
  if (state.captionDragScope) return state.captionDragScope;
  try {
    return localStorage.getItem(SCOPE_KEY) === "style" ? "style" : "one";
  } catch {
    return "one";
  }
}

export function setDragScope(scope) {
  setState({ captionDragScope: scope });
  try {
    localStorage.setItem(SCOPE_KEY, scope);
  } catch {
    // Convenience only; the choice still holds for this session.
  }
}

// ---------------------------------------------------------------- ops
// The built-in look becomes a real project style (and the default) before it can be edited or dragged.
function materialize(legacy, patch = {}) {
  const id = newId("cs");
  return {
    id,
    list: [
      { op: "captionStyle.add", item: { ...DEFAULT_STYLE, size: legacy, ...patch, id } },
      { op: "project.update", patch: { settings: { captionStyle: id } } },
    ],
  };
}

// Change a style; id null edits the built-in look by turning it into the project default first.
export function editStyleOps(styleId, patch, legacy) {
  if (styleId) return { id: styleId, list: [{ op: "captionStyle.update", id: styleId, patch }] };
  return materialize(legacy, patch);
}

export function addStyleOps(project, base, name) {
  const id = newId("cs");
  const { id: _drop, ...fields } = base || DEFAULT_STYLE;
  const list = [{ op: "captionStyle.add", item: { ...fields, name, id } }];
  // The first style becomes the default, so it takes over from the built-in look right away.
  if (!defaultStyleId(project)) list.push({ op: "project.update", patch: { settings: { captionStyle: id } } });
  return { id, list };
}

export const setDefaultOps = (id) => [{ op: "project.update", patch: { settings: { captionStyle: id } } }];

// Every caption uses the style: it becomes the default and the captions drop their own choice.
export function useForAllOps(project, id) {
  return [
    ...setDefaultOps(id),
    ...(project.captions || []).filter((c) => c.style && c.style !== id)
      .map((c) => ({ op: "caption.update", id: c.id, patch: { style: null } })),
  ];
}

// A caption dropped at point: just this caption, or every caption of its style (their own drags cleared).
export function moveOps(project, cap, point, scope, legacy) {
  const x = round(point.x, 4);
  const y = round(point.y, 4);
  if (scope !== "style") return [{ op: "caption.update", id: cap.id, patch: { x, y } }];
  const styleId = resolveStyle(project, cap, legacy).id;
  const followers = (project.captions || [])
    .filter((c) => (c.x != null || c.y != null) && usesStyle(project, c, styleId, legacy))
    .map((c) => ({ op: "caption.update", id: c.id, patch: { x: null, y: null } }));
  return [...editStyleOps(styleId, { x, y }, legacy).list, ...followers];
}
