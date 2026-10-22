// Built-in caption looks offered in the caption style dialog. Each lists only what differs from DEFAULT_STYLE
// (lib/caption-style.js); adding one to a project copies it as an ordinary, editable style.
// No imports, so tests/test_captions.py can load this file with node and validate every preset.
export const PRESET_STYLES = [
  // The owner's everyday look, matched to a burnt-in reference frame: glyphs 54px high, bottom at y=1025.
  { name: "常用：白字細黑框", size: 84, outlineWidth: 2.5, shadow: false, marginV: 42 },
  { name: "經典白字黑框" },
  { name: "黑底白字", bold: false, size: 56, outline: false, shadow: false, box: true, boxOpacity: 0.65, boxPadding: 14 },
  { name: "黃字黑框", color: "#ffd400", size: 64, outlineWidth: 5, shadowOpacity: 0.6, shadowDistance: 2 },
  { name: "大字易讀", size: 96, outlineWidth: 6, shadowOpacity: 0.5, shadowDistance: 2, marginV: 50 },
  { name: "電影感細字", bold: false, size: 48, spacing: 2, outline: false, shadowOpacity: 0.8, shadowDistance: 2, shadowBlur: 3 },
  { name: "新聞字卡", size: 56, outline: false, shadow: false, box: true, boxColor: "#0b3a75", boxOpacity: 0.9, boxPadding: 16 },
];
