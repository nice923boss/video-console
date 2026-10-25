// Edit workspace layouts (UI-08): which panels show and how big, saved under a name and picked from the
// player bar. Applying one writes the panel prefs and the open panels follow (lib/prefs.js writePref).
import { readPref, writePref } from "../lib/prefs.js";
import { openMenu, promptDialog } from "./overlay.js";
import { toast } from "./toast.js";

// The panel prefs a layout holds, with the values the panels start from.
const DEFAULTS = { "vc.edit.tlh": 280, "vc.edit.meters": true, "vc.edit.metersW": 88, "vc.edit.bin": false, "vc.edit.scopes": false };
const BUILT_IN = [
  { name: "標準", hint: "時間軸中等、音量表開", prefs: DEFAULTS },
  { name: "剪輯", hint: "時間軸加大、素材清單開", prefs: { ...DEFAULTS, "vc.edit.tlh": 400, "vc.edit.bin": true } },
  { name: "看片", hint: "預覽畫面最大", prefs: { ...DEFAULTS, "vc.edit.tlh": 170, "vc.edit.meters": false } },
];
const LIST_KEY = "vc.edit.layouts";
const MAX_SAVED = 10;
const MAX_NAME = 20;

const validList = (v) => Array.isArray(v) && v.every((l) => l && typeof l.name === "string" && l.prefs && typeof l.prefs === "object");
const savedLayouts = () => readPref(LIST_KEY, [], validList);
const current = () => Object.fromEntries(Object.entries(DEFAULTS).map(([k, d]) => [k, readPref(k, d, (v) => v != null)]));
const matches = (prefs, now) => Object.keys(DEFAULTS).every((k) => (k in prefs ? prefs[k] : DEFAULTS[k]) === now[k]);

function apply(prefs) {
  for (const k of Object.keys(DEFAULTS)) if (k in prefs) writePref(k, prefs[k]);
}

async function saveCurrent() {
  const saved = savedLayouts();
  const name = await promptDialog({ title: "儲存目前版面", label: "版面名稱", value: `我的版面 ${saved.length + 1}`,
    confirmLabel: "儲存", hint: "記下時間軸高度、音量表、素材清單、示波器；同名的版面會被取代",
    validate: (v) => {
      const n = v.trim();
      if (!n) return "請輸入名稱";
      if (n.length > MAX_NAME) return `名稱最多 ${MAX_NAME} 個字`;
      return BUILT_IN.some((l) => l.name === n) ? "這是內建版面的名稱，請換一個" : "";
    } });
  if (name === null) return;
  const n = name.trim();
  const rest = saved.filter((l) => l.name !== n);
  if (rest.length >= MAX_SAVED) {
    toast(`最多存 ${MAX_SAVED} 個版面，請先刪除不用的`, { level: "warn" });
    return;
  }
  writePref(LIST_KEY, [...rest, { name: n, prefs: current() }]);
  toast(`已儲存版面「${n}」`, { level: "success" });
}

function removeMenu(anchor) {
  openMenu(anchor, [
    { header: "刪除哪個版面？" },
    ...savedLayouts().map((l) => ({ label: l.name, icon: "trash", danger: true,
      onClick: () => writePref(LIST_KEY, savedLayouts().filter((x) => x.name !== l.name)) })),
  ]);
}

export function layoutMenu(e) {
  const anchor = e.currentTarget;
  const now = current();
  const saved = savedLayouts();
  const entry = (l) => ({ label: l.name, hint: l.hint || "", checked: matches(l.prefs, now), onClick: () => apply(l.prefs) });
  openMenu(e, [
    { header: "工作區版面" },
    ...BUILT_IN.map(entry),
    saved.length > 0 && { divider: true },
    ...saved.map(entry),
    { divider: true },
    { label: "儲存目前版面…", icon: "save", onClick: saveCurrent },
    saved.length > 0 && { label: "刪除已存版面…", icon: "trash", onClick: () => removeMenu(anchor) },
  ]);
}
