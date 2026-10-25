// Rebindable keyboard commands: each has its keys in every preset, and the viewer's own changes live in
// settings.ui.keymap = {preset, overrides: [[id, [combo, …]], …]} (a list, so a removed key stays removed
// through the server's dict merge). Views bind handlers by command id with useKeymap, so a rebind
// reaches them without code changes.
import { useEffect } from "./h.js";
import { getState, useStore } from "./store.js";
import { comboOf, useHotkeys } from "./keys.js";
import { comboLabel } from "../components/ui.js";

export const PRESETS = [
  { value: "default", label: "Video Console" },
  { value: "fcpx", label: "Final Cut Pro" },
];

export const PALETTE_GROUP = "指令面板";
export const GROUPS = ["全部頁面", PALETTE_GROUP, "剪輯", "分鏡", "素材庫", "對話框"];
const SCOPE_GROUP = { global: "全部頁面", edit: "剪輯", board: "分鏡", library: "素材庫", dialog: "對話框" };

// Chromium handles these before the page sees them, or ignores preventDefault on them.
export const RESERVED = new Set(["mod+t", "mod+w", "mod+n", "mod+shift+t", "mod+shift+n", "mod+shift+w",
  "mod+tab", "mod+shift+tab", "alt+f4"]);

// fcpx: keys in the Final Cut Pro preset when they differ (Command is Ctrl and Option is Alt on Windows).
const cmd = (scope, id, label, keys, fcpx = keys) => ({ scope, group: SCOPE_GROUP[scope], id, label, keys, fcpx });

const BUILT_IN = [
  cmd("global", "app.palette", "指令面板：搜尋指令、鏡頭與專案", ["mod+shift+p"]),
  cmd("global", "app.undo", "復原", ["mod+z"]),
  cmd("global", "app.redo", "重做", ["mod+shift+z", "mod+y"]),

  cmd("edit", "edit.play", "播放或暫停", ["space"]),
  cmd("edit", "edit.reverse", "倒著播放", ["j"], ["j", "shift+space"]),
  cmd("edit", "edit.stop", "停止", [], ["k"]),
  cmd("edit", "edit.forward", "往前播放", ["l"]),
  cmd("edit", "edit.prevFrame", "前一格", ["arrowleft"]),
  cmd("edit", "edit.nextFrame", "後一格", ["arrowright"]),
  cmd("edit", "edit.back1s", "倒退 1 秒", ["shift+arrowleft"], []),
  cmd("edit", "edit.fwd1s", "前進 1 秒", ["shift+arrowright"], []),
  cmd("edit", "edit.back10f", "倒退 10 格", [], ["shift+arrowleft"]),
  cmd("edit", "edit.fwd10f", "前進 10 格", [], ["shift+arrowright"]),
  cmd("edit", "edit.prevEdit", "上一個鏡頭（剪接點）", ["arrowup"], ["arrowup", ";"]),
  cmd("edit", "edit.nextEdit", "下一個鏡頭（剪接點）", ["arrowdown"], ["arrowdown", "'"]),
  cmd("edit", "edit.start", "跳到開頭", ["home"]),
  cmd("edit", "edit.end", "跳到結尾", ["end"]),
  cmd("edit", "edit.split", "在播放頭分割片段", ["alt+b"], ["mod+b"]),
  cmd("edit", "edit.splitAll", "在播放頭分割所有軌道", ["alt+shift+b"], ["mod+shift+b"]),
  cmd("edit", "edit.trimStart", "向左裁剪（刪掉播放頭前面的部分）", ["f1"], ["alt+["]),
  cmd("edit", "edit.trimEnd", "向右裁剪（刪掉播放頭後面的部分）", ["f3"], ["alt+]"]),
  cmd("edit", "edit.toolSelect", "選擇模式", ["a"]),
  cmd("edit", "edit.toolBlade", "分割模式：點片段就在點的位置切開", ["b"]),
  cmd("edit", "edit.toolTrim", "修剪模式：滾動剪接點、滑動內容、滑移鏡頭", ["t"]),
  cmd("edit", "edit.toggleEnabled", "啟用或停用選取的片段", ["v"]),
  cmd("edit", "edit.addCaption", "在播放頭新增字幕", ["alt+c"]),
  cmd("edit", "edit.markerAdd", "在播放頭新增標記", ["m"]),
  cmd("edit", "edit.markerColor", "切換播放頭處標記的顏色", ["alt+m"], []),
  cmd("edit", "edit.markerNext", "跳到下一個標記", ["f8"], []),
  cmd("edit", "edit.markerPrev", "跳到上一個標記", ["f7"], []),
  cmd("edit", "edit.volumeUp", "選取片段音量加 1dB", ["mod+."], []),
  cmd("edit", "edit.volumeDown", "選取片段音量減 1dB", ["mod+,"], []),
  cmd("edit", "edit.meters", "顯示或隱藏音量表", ["mod+shift+8"]),
  cmd("edit", "edit.bin", "顯示或隱藏素材清單", ["mod+shift+1"]),
  cmd("edit", "edit.scopes", "顯示或隱藏示波器", ["mod+shift+7"]),
  cmd("edit", "edit.markIn", "設定區域入點", ["i"]),
  cmd("edit", "edit.markOut", "設定區域出點", ["o"]),
  cmd("edit", "edit.rangeFromClip", "以選取片段設定區域", ["shift+x"], ["x"]),
  cmd("edit", "edit.playRange", "播放區域（入點到出點）", ["shift+enter"], ["/"]),
  cmd("edit", "edit.clearRange", "清除區域", ["alt+x"]),
  cmd("edit", "edit.freeze", "定格：在播放頭插入一段靜止畫面", ["alt+shift+f"], ["alt+f"]),
  cmd("edit", "edit.detachAudio", "分離或還原鏡頭的原聲", ["mod+shift+s"], []),
  cmd("edit", "edit.reverseClip", "倒放選取的鏡頭", ["mod+shift+r"], []),
  cmd("edit", "edit.speed", "變速：設定選取鏡頭的速度", ["mod+r"]),
  cmd("edit", "edit.speedCurve", "曲線變速", ["shift+b"], []),
  cmd("edit", "edit.keyframePanel", "顯示或隱藏關鍵幀面板", ["mod+k"], []),
  cmd("edit", "edit.keyframeAdd", "在播放頭添加關鍵幀", ["k"], ["alt+k"]),
  cmd("edit", "edit.group", "組合", ["alt+g"], []),
  cmd("edit", "edit.compound", "新建複合片段", ["mod+shift+g"], ["alt+g"]),
  cmd("edit", "edit.uncompound", "解除複合片段", ["alt+shift+g"], ["mod+shift+g"]),
  cmd("edit", "edit.precompose", "預合成", ["alt+e"], []),
  cmd("edit", "edit.zoomIn", "放大時間軸", ["f6"], ["mod+="]),
  cmd("edit", "edit.zoomOut", "縮小時間軸", ["f5"], ["mod+-"]),
  cmd("edit", "edit.zoomFit", "時間軸符合視窗寬度", ["shift+z"]),
  cmd("edit", "edit.trackTaller", "增高軌道", ["+"], []),
  cmd("edit", "edit.trackShorter", "降低軌道", ["_"], []),
  cmd("edit", "edit.snap", "開關磁吸對齊", ["n"]),
  cmd("edit", "edit.link", "開關連動上下軌", ["shift+n"]),
  cmd("edit", "edit.copy", "複製選取的片段", ["mod+c"]),
  cmd("edit", "edit.paste", "在播放頭貼上（鏡頭插在播放頭所在鏡頭之後）", ["mod+v"]),
  cmd("edit", "edit.delete", "刪除選取的片段", ["delete", "backspace", "f2"], ["delete", "backspace"]),
  cmd("edit", "edit.rippleDelete", "漣漪刪除（後面的片段往前補位）", ["shift+delete", "shift+backspace"]),
  cmd("edit", "edit.deselect", "取消選取片段", ["escape"]),

  cmd("board", "board.add", "新增鏡頭", ["n"]),
  cmd("board", "board.image", "選取的鏡頭生圖", ["g"]),
  cmd("board", "board.video", "選取的鏡頭生影片", ["v"]),
  cmd("board", "board.preview", "預覽鏡頭", ["space"]),
  cmd("board", "board.duplicate", "複製選取的鏡頭", ["mod+d"]),
  cmd("board", "board.delete", "刪除選取的鏡頭", ["delete", "backspace"]),
  cmd("board", "board.selectAll", "全選", ["mod+a"]),
  cmd("board", "board.deselect", "取消多選", ["escape"]),
  cmd("board", "board.left", "焦點往左", ["arrowleft"]),
  cmd("board", "board.right", "焦點往右", ["arrowright"]),
  cmd("board", "board.up", "焦點往上", ["arrowup"]),
  cmd("board", "board.down", "焦點往下", ["arrowdown"]),
  cmd("board", "board.first", "跳到第一個鏡頭", ["home"]),
  cmd("board", "board.last", "跳到最後一個鏡頭", ["end"]),
  cmd("board", "board.moveBack", "把鏡頭往前移", ["mod+arrowleft"]),
  cmd("board", "board.moveForward", "把鏡頭往後移", ["mod+arrowright"]),

  cmd("library", "library.preview", "預覽", ["space", "enter"]),
  cmd("library", "library.favorite", "加入或取消收藏", ["f"]),
  cmd("library", "library.rename", "重新命名", ["f2"]),
  cmd("library", "library.delete", "移除素材", ["delete", "backspace"]),
  cmd("library", "library.prev", "選前一個", ["arrowleft"]),
  cmd("library", "library.next", "選後一個", ["arrowright"]),
  cmd("library", "library.first", "選第一個", ["home"]),
  cmd("library", "library.last", "選最後一個", ["end"]),
  cmd("library", "library.deselect", "取消選取", ["escape"]),
  cmd("library", "library.selectAll", "全選", ["mod+a"]),

  // Inside dialogs (UI-05); bound with useDialogKeys.
  cmd("dialog", "dialog.close", "關閉對話框或選單", ["escape"]),
  cmd("dialog", "dialog.confirm", "送出或套用（文字輸入框、AI 潤飾結果、劇本匯入；單行輸入框也可按 Enter）", ["mod+enter"]),
  cmd("dialog", "dialog.prev", "預覽視窗：上一張", ["arrowleft"]),
  cmd("dialog", "dialog.next", "預覽視窗：下一張", ["arrowright"]),
  cmd("dialog", "dialog.info", "預覽視窗：顯示或隱藏資訊", ["i"]),
];

const registry = new Map(BUILT_IN.map((c) => [c.id, c]));

// Command palette entries that can take a key too: [{id, label, keys?}].
export function defineCommands(list) {
  for (const c of list) {
    const keys = c.keys || [];
    registry.set(c.id, { scope: "global", group: PALETTE_GROUP, id: c.id, label: c.label, keys, fcpx: keys });
  }
}

export const allCommands = () => [...registry.values()];
export const commandOf = (id) => registry.get(id);

export const selectKeymap = (s) => (s.settings && s.settings.ui && s.settings.ui.keymap) || null;

const validOverride = (o) => Array.isArray(o) && typeof o[0] === "string" && Array.isArray(o[1])
  && o[1].every((k) => typeof k === "string" && k);

// raw: settings.ui.keymap → {preset, overrides: Map(id → combos)}.
export function readKeymap(raw = selectKeymap(getState())) {
  const preset = raw && raw.preset === "fcpx" ? "fcpx" : "default";
  const overrides = new Map(raw && Array.isArray(raw.overrides) ? raw.overrides.filter(validOverride) : []);
  return { preset, overrides };
}

export const presetKeys = (c, preset) => (preset === "fcpx" ? c.fcpx : c.keys);

export function keysOf(id, km = readKeymap()) {
  if (km.overrides.has(id)) return km.overrides.get(id);
  const c = registry.get(id);
  return c ? presetKeys(c, km.preset) : [];
}

export const keyLabel = (id) => {
  const k = keysOf(id)[0];
  return k ? comboLabel(k) : "";
};

// "按 Ctrl+Z " for sentences about undoing, following the undo key in use, or the toolbar button without one.
export const undoKeyText = () => {
  const k = keyLabel("app.undo");
  return k ? `按 ${k} ` : "按上方的復原鈕";
};

// Foot of a clip's properties, like "Delete 刪除，Esc 回到鏡頭屬性", read from the keys in use.
export const clipKeysHint = (verb) => [[keyLabel("edit.delete"), verb], [keyLabel("edit.deselect"), "回到鏡頭屬性"]]
  .filter(([k]) => k).map(([k, v]) => `${k} ${v}`).join("，");

// "Ctrl+Enter 儲存" for a dialog hint, following the confirm key in use, or nothing without one.
export const confirmKeyText = (verb) => {
  const k = keyLabel("dialog.confirm");
  return k ? `${k} ${verb}` : "";
};

// Tooltip suffix like "（Ctrl+B）", or nothing when the command has no key.
export const keyHint = (id) => {
  const label = keyLabel(id);
  return label ? `（${label}）` : "";
};

// An open dialog hides the page keys, so dialog keys only meet each other.
const overlaps = (a, b) => a === b || (a !== "dialog" && b !== "dialog" && (a === "global" || b === "global"));

// Other commands on the same key that can be active at the same time.
export function conflictsOf(id, combo, km) {
  const scope = registry.get(id).scope;
  return allCommands().filter((o) => o.id !== id && overlaps(scope, o.scope) && keysOf(o.id, km).includes(combo));
}

// When several commands on screen share a key, a page's own command beats an app-wide one, and
// otherwise the one listed first in the shortcut settings wins.
let order = null; // id -> position in the registry, rebuilt when palette commands are added
function rank(id) {
  if (!order || order.size !== registry.size) order = new Map([...registry.keys()].map((k, i) => [k, i]));
  const c = registry.get(id); // a handler for an id nobody registered never has a key anyway
  return c ? (c.scope === "global" ? registry.size : 0) + order.get(id) : 2 * registry.size;
}
export const winnerOf = (ids) => ids.reduce((a, b) => (rank(b) < rank(a) ? b : a));

const mounted = new Map(); // command id -> how many mounted useKeymap callers bind it
function takerOf(combo, km) {
  const ids = [...mounted].filter(([id, n]) => n > 0 && keysOf(id, km).includes(combo)).map(([id]) => id);
  return ids.length ? winnerOf(ids) : null;
}

// handlers: {commandId: fn | {fn, allowInInput}}. Reading the keymap from the store re-renders the
// caller after a rebind, which hands useHotkeys the new map. Each caller is its own hotkey layer, so a
// key shared with a command in another layer passes through unless this command is the one that wins.
export function useKeymap(handlers, opts) {
  const km = readKeymap(useStore(selectKeymap));
  const ids = Object.keys(handlers).sort((a, b) => rank(a) - rank(b));
  const active = !opts || opts.active !== false;
  useEffect(() => {
    if (!active) return undefined;
    for (const id of ids) mounted.set(id, (mounted.get(id) || 0) + 1);
    return () => { for (const id of ids) mounted.set(id, mounted.get(id) - 1); };
  }, [active, ids.join()]);
  const map = {};
  for (const id of ids) {
    const handler = handlers[id];
    const fn = typeof handler === "function" ? handler : handler.fn;
    for (const combo of keysOf(id, km)) {
      if (map[combo]) continue;
      const guarded = (e) => (takerOf(combo, km) === id ? fn(e) : false);
      map[combo] = typeof handler === "function" ? guarded : { ...handler, fn: guarded };
    }
  }
  useHotkeys(map, opts);
}

// A bare printable key types into a text box, so as a dialog key it never fires while typing.
const printable = (combo) => combo === "space" || /^(shift\+)?.$/.test(combo);

// Keys inside a dialog (UI-05). handlers: {commandId | combo: fn | {fn, allowInInput}}; a command id
// follows the keymap, a plain combo (list navigation) is fixed and wins a shared key. Among commands a
// shared key goes to the one listed first in the shortcut settings.
export function useDialogKeys(handlers, opts) {
  const km = readKeymap(useStore(selectKeymap));
  const map = {};
  for (const c of allCommands()) {
    const handler = handlers[c.id];
    if (!handler) continue;
    for (const combo of keysOf(c.id, km)) {
      if (!map[combo]) map[combo] = typeof handler === "function" || !printable(combo) ? handler : handler.fn;
    }
  }
  for (const [combo, handler] of Object.entries(handlers)) if (!registry.has(combo)) map[combo] = handler;
  useHotkeys(map, opts);
}

// Whether a key pressed in a dialog's text box is the confirm key.
export const isConfirmKey = (e) => {
  const combo = comboOf(e);
  return !printable(combo) && keysOf("dialog.confirm").includes(combo);
};
