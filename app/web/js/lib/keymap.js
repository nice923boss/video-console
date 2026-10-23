// Rebindable keyboard commands: each has its keys in every preset, and the viewer's own changes live in
// settings.ui.keymap = {preset, overrides: [[id, [combo, …]], …]} (a list, so a removed key stays removed
// through the server's dict merge). Views bind handlers by command id with useKeymap, so a rebind
// reaches them without code changes.
import { getState, useStore } from "./store.js";
import { useHotkeys } from "./keys.js";
import { comboLabel } from "../components/ui.js";

export const PRESETS = [
  { value: "default", label: "Video Console" },
  { value: "fcpx", label: "Final Cut Pro" },
];

export const PALETTE_GROUP = "指令面板";
export const GROUPS = ["全部頁面", PALETTE_GROUP, "剪輯", "分鏡", "素材庫"];
const SCOPE_GROUP = { global: "全部頁面", edit: "剪輯", board: "分鏡", library: "素材庫" };

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
  cmd("edit", "edit.toggleEnabled", "啟用或停用選取的片段", ["v"]),
  cmd("edit", "edit.addCaption", "在播放頭新增字幕", ["alt+c"]),
  cmd("edit", "edit.markerAdd", "在播放頭新增標記", ["m"]),
  cmd("edit", "edit.markerColor", "切換播放頭處標記的顏色", ["alt+m"], []),
  cmd("edit", "edit.markerNext", "跳到下一個標記", ["f8"], []),
  cmd("edit", "edit.markerPrev", "跳到上一個標記", ["f7"], []),
  cmd("edit", "edit.volumeUp", "選取片段音量加 1dB", ["mod+."], []),
  cmd("edit", "edit.volumeDown", "選取片段音量減 1dB", ["mod+,"], []),
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
  cmd("edit", "edit.copy", "複製選取的音訊片段", ["mod+c"]),
  cmd("edit", "edit.paste", "在播放頭貼上（音訊片段放到新的音軌）", ["mod+v"]),
  cmd("edit", "edit.delete", "刪除選取的片段", ["delete", "backspace", "f2"], ["delete", "backspace"]),
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

// Tooltip suffix like "（Ctrl+B）", or nothing when the command has no key.
export const keyHint = (id) => {
  const label = keyLabel(id);
  return label ? `（${label}）` : "";
};

const overlaps = (a, b) => a === b || a === "global" || b === "global";

// Other commands on the same key that can be active at the same time.
export function conflictsOf(id, combo, km) {
  const scope = registry.get(id).scope;
  return allCommands().filter((o) => o.id !== id && overlaps(scope, o.scope) && keysOf(o.id, km).includes(combo));
}

// handlers: {commandId: fn | {fn, allowInInput}}. Reading the keymap from the store re-renders the
// caller after a rebind, which hands useHotkeys the new map.
export function useKeymap(handlers, opts) {
  const km = readKeymap(useStore(selectKeymap));
  const map = {};
  for (const [id, handler] of Object.entries(handlers)) {
    for (const combo of keysOf(id, km)) if (!map[combo]) map[combo] = handler;
  }
  useHotkeys(map, opts);
}
