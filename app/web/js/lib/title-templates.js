// Title card templates (GR-10): opening titles, chapter cards and lower thirds. Each part of a template is a
// caption with its own style pinned at an anchor (title: true), so the preview and the burnt-in export draw it
// like any subtitle, and it is changed afterwards in the caption panel and the style dialog.
import { DEFAULT_STYLE, MAX_STYLES, uniqueName } from "./caption-style.js";
import { newId, round } from "./format.js";

const LOOK_KEYS = Object.keys(DEFAULT_STYLE).filter((k) => k !== "name");

// Anchors are fractions of the frame (x, y is the line's middle); sizes are pixels on a 1080-high frame. The
// parts of one template sit far enough apart that the caption stacking never moves them.
export const TEMPLATES = [
  { id: "opening", name: "片頭標題", hint: "置中的大標題，下面一行副標", len: 4, parts: [
    { key: "main", label: "主標", text: "影片標題",
      style: { size: 110, outline: false, shadowOpacity: 0.6, shadowDistance: 3, shadowBlur: 4, x: 0.5, y: 0.46 } },
    { key: "sub", label: "副標", text: "副標題或日期",
      style: { size: 46, bold: false, spacing: 6, color: "#f2d27a", outline: false, shadowOpacity: 0.6, shadowDistance: 2,
        shadowBlur: 3, x: 0.5, y: 0.585 } },
  ] },
  { id: "chapter", name: "章節標題", hint: "畫面中央半透明底的章節名稱", len: 3, parts: [
    { key: "main", label: "章節", text: "第一章　開場",
      style: { size: 72, outline: false, shadow: false, box: true, boxColor: "#000000", boxOpacity: 0.6, boxPadding: 28, x: 0.5, y: 0.5 } },
  ] },
  { id: "lower-box", name: "下標字卡（色塊）", hint: "左下角姓名與職稱，各有底色", len: 5, parts: [
    { key: "name", label: "姓名", text: "王小明",
      style: { size: 58, outline: false, shadow: false, box: true, boxColor: "#0b3a75", boxOpacity: 0.92, boxPadding: 14,
        align: "left", x: 0.06, y: 0.78 } },
    { key: "role", label: "職稱", text: "產品經理",
      style: { size: 38, bold: false, color: "#0b3a75", outline: false, shadow: false, box: true, boxColor: "#ffffff",
        boxOpacity: 0.92, boxPadding: 10, align: "left", x: 0.06, y: 0.865 } },
  ] },
  { id: "lower-plain", name: "下標字卡（簡潔）", hint: "左下角姓名與職稱，不加底色", len: 5, parts: [
    { key: "name", label: "姓名", text: "王小明",
      style: { size: 60, outlineWidth: 2, shadowOpacity: 0.6, shadowDistance: 2, align: "left", x: 0.06, y: 0.79 } },
    { key: "role", label: "職稱", text: "產品經理",
      style: { size: 40, bold: false, color: "#ffd400", outlineWidth: 2, shadowOpacity: 0.6, shadowDistance: 2,
        align: "left", x: 0.06, y: 0.86 } },
  ] },
  { id: "corner", name: "角落標籤", hint: "左上角的地點、日期或節目名", len: 5, parts: [
    { key: "main", label: "標籤", text: "台北．2026",
      style: { size: 40, outline: false, shadow: false, box: true, boxColor: "#c8102e", boxOpacity: 1, boxPadding: 10,
        align: "left", x: 0.05, y: 0.09 } },
  ] },
];

// A part's full caption style, with the colours the user picked (v: {color, boxColor}).
export function lookOf(tpl, part, v = {}) {
  const look = { ...DEFAULT_STYLE, ...part.style, name: `${tpl.name}：${part.label}` };
  return { ...look, color: v.color || look.color, boxColor: v.boxColor || look.boxColor };
}

// What the dialog starts with: each part's sample text and colours.
export const startValues = (tpl) => Object.fromEntries(tpl.parts.map((p) => {
  const look = lookOf(tpl, p);
  return [p.key, { text: p.text, color: look.color, boxColor: look.boxColor }];
}));

const sameLook = (a, b) => LOOK_KEYS.every((k) => a[k] === b[k]);

// The ops that add the template at `start` for `len` seconds: a style per part (one already in the project
// that looks the same is reused, so adding a template twice does not pile up styles) and a caption per part
// with text. Never touches the project's default style. {list, first} or {error}.
export function titleOps(project, tpl, values, start, len = tpl.len) {
  const styles = [...(project.captionStyles || [])];
  const list = [];
  const caps = [];
  for (const part of tpl.parts) {
    const text = ((values[part.key] || {}).text || "").trim();
    if (!text) continue;
    const look = lookOf(tpl, part, values[part.key]);
    let style = styles.find((s) => sameLook(s, look));
    if (!style) {
      style = { ...look, id: newId("cs"), name: uniqueName({ captionStyles: styles }, look.name) };
      styles.push(style);
      list.push({ op: "captionStyle.add", item: style });
    }
    caps.push({ op: "caption.add", item: { id: newId("c"), start: round(start, 3), end: round(start + len, 3), text,
      position: "middle", style: style.id, title: true } });
  }
  if (!caps.length) return { error: "請輸入標題文字" };
  if (styles.length > MAX_STYLES) return { error: `字幕樣式最多 ${MAX_STYLES} 個，請先在字幕樣式刪掉用不到的` };
  return { list: [...list, ...caps], first: caps[0].item.id };
}
