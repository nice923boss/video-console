// Right-click on the edit timeline: each kind of clip gets a menu of what can be done to it, in place of the
// browser's own menu, which also stays away from the rest of the timeline (text fields keep theirs).
// The items run the same commands as the timeline keys, on the clip that was right-clicked.
import { ops, deleteShots, updateShot, focusShot } from "../lib/actions.js";
import { getState, setState } from "../lib/store.js";
import { clock } from "../lib/clock.js";
import { keyLabel } from "../lib/keymap.js";
import { rippleOps } from "../lib/ripple.js";
import { copyItem, pasteItem } from "./tl-clipboard.js";
import { splitAtPlayhead } from "./tl-edit.js";
import {
  current, OFF_FIELD, KEYABLE, toggleEnabled, rangeFromClip, freezeFrame, detachAudio, reverseClip, isReverse, focusSpeed,
  addKeyframe,
} from "./tl-commands.js";
import { openMenu } from "./overlay.js";
import { labelColors } from "./insp-content.js";

const NAMES = { shot: "鏡頭", audio: "音訊片段", overlay: "上方畫面", caption: "字幕", adjust: "調整圖層" };
const OFF_LABELS = { disabled: ["停用片段", "啟用片段"], muted: ["靜音", "取消靜音"], hidden: ["隱藏", "顯示"] };
const PLAYHEAD_OFF = "先把播放頭移到這個片段上";
const EPS = 1e-3;

export function deleteSelection(sel) {
  if (!sel) return false;
  if (sel.type === "shot") deleteShots([sel.id]);
  else if (sel.type === "caption") ops([{ op: "caption.delete", id: sel.id }], { label: "刪除字幕" });
  else if (sel.type === "audio") ops([{ op: "audio.delete", id: sel.id }], { label: "移除音軌" });
  else if (sel.type === "overlay") ops([{ op: "overlay.delete", id: sel.id }], { label: "移除片段" });
  else if (sel.type === "adjust") ops([{ op: "adjust.delete", id: sel.id }], { label: "刪除調整圖層" });
  else return false;
  return true;
}

// Shift+Delete: what comes after the clip on its track moves back to close the gap.
export function rippleDelete(project, sel) {
  if (!sel) return false;
  if (sel.type === "shot") return deleteShots([sel.id], { ripple: true }), true;
  const list = rippleOps(project, sel);
  if (!list) return false;
  ops(list, { label: "漣漪刪除" });
  return true;
}

// The picture commands that only a video shot takes: still frame, its sound, reverse, speed.
function shotItems(cur, run, at) {
  const clip = cur.clip;
  const video = !!clip && clip.source === "video";
  const notVideo = video ? undefined : "只有影片鏡頭可以用";
  const detached = !!cur.item.audioDetached;
  return [
    { divider: true },
    { label: "添加靜幀（5 秒）", icon: "image", hint: keyLabel("edit.freeze"), onClick: freezeFrame,
      disabled: !video || !at, title: notVideo || (at ? "在播放頭插入 5 秒靜止畫面，後面的畫面接在靜幀之後" : PLAYHEAD_OFF) },
    { label: detached ? "還原原聲" : "分離原聲", icon: "music", hint: keyLabel("edit.detachAudio"), onClick: run(detachAudio),
      disabled: !detached && !(video && clip.take.hasAudio), title: detached || (video && clip.take.hasAudio) ? undefined : "這個鏡頭沒有原聲" },
    { label: "倒放", checked: video && isReverse(clip.take), hint: keyLabel("edit.reverseClip"), onClick: run(reverseClip),
      disabled: !video, title: notVideo },
    { label: "變速…", icon: "gauge", hint: keyLabel("edit.speed"), onClick: run(focusSpeed), disabled: !video, title: notVideo },
  ];
}

// A shot under a full-frame upper-track clip: its picture shows black while its sound plays.
function pictureItem(shot) {
  const hidden = !!shot.pictureHidden;
  return { label: hidden ? "顯示分鏡畫面" : "隱藏分鏡畫面（原聲保留）", icon: hidden ? "eye" : "eye-off",
    onClick: () => updateShot(shot.id, { pictureHidden: !hidden }, { label: hidden ? "顯示分鏡畫面" : "隱藏分鏡畫面" }) };
}

function colorItems(shot) {
  const now = shot.color ?? null;
  return [
    { divider: true },
    { header: "標籤顏色" },
    ...labelColors(getState().settings).map((c) => ({ label: c.label, swatch: c.value, checked: now === c.value,
      onClick: () => now !== c.value && updateShot(shot.id, { color: c.value }, { label: "修改標籤顏色" }) })),
  ];
}

function clipItems(data, sel, cur) {
  const { project } = data();
  const run = (fn) => () => fn(data().project, data().clips, sel);
  const t = clock.time;
  const inside = t > cur.start + EPS && t < cur.start + cur.length - EPS;
  const at = t >= cur.start - EPS && t <= cur.start + cur.length + EPS;
  const field = OFF_FIELD[sel.type];
  const [offLabel, onLabel] = OFF_LABELS[field];
  const detachedSound = sel.type === "audio" && !!cur.item.shotId;
  return [
    { header: NAMES[sel.type] },
    { label: "複製", icon: "copy", hint: keyLabel("edit.copy"), onClick: run(copyItem) },
    { label: "在播放頭貼上", icon: "clipboard", hint: keyLabel("edit.paste"), onClick: () => pasteItem(data().project, data().clips) },
    { label: "在播放頭分割", icon: "scissors", hint: keyLabel("edit.split"), onClick: run(splitAtPlayhead),
      disabled: !inside, title: inside ? undefined : PLAYHEAD_OFF },
    ...(sel.type === "shot" ? shotItems(cur, run, at) : []),
    ...(detachedSound ? [{ label: "還原原聲", icon: "music", hint: keyLabel("edit.detachAudio"), onClick: run(detachAudio) }] : []),
    ...(KEYABLE.includes(sel.type) ? [{ label: "添加關鍵幀", icon: "diamond", hint: keyLabel("edit.keyframeAdd"),
      onClick: run(addKeyframe), disabled: !at, title: at ? undefined : PLAYHEAD_OFF }] : []),
    { divider: true },
    { label: cur.item[field] ? onLabel : offLabel, icon: cur.item[field] ? "eye" : "eye-off", hint: keyLabel("edit.toggleEnabled"),
      onClick: run(toggleEnabled) },
    ...(sel.type === "shot" ? [pictureItem(cur.item)] : []),
    { label: "以片段設定區域", icon: "range-x", hint: keyLabel("edit.rangeFromClip"), onClick: run(rangeFromClip) },
    { divider: true },
    { label: "刪除", icon: "trash", danger: true, hint: keyLabel("edit.delete"), onClick: () => deleteSelection(sel) },
    { label: "漣漪刪除", icon: "trash", danger: true, hint: keyLabel("edit.rippleDelete"),
      onClick: () => rippleDelete(data().project, sel), disabled: sel.type !== "shot" && !rippleOps(project, sel) },
    ...(sel.type === "shot" ? colorItems(cur.item) : []),
  ];
}

// onContextMenu on the timeline canvas. data(): the project and sequence as they are now, so an item picked
// after the project changed acts on the current one.
export function timelineMenu(e, data) {
  if (e.defaultPrevented || e.target.closest("input, textarea, [contenteditable]")) return;
  e.preventDefault();
  const el = e.target.closest("[data-sel]");
  if (!el) return;
  const [type, id] = el.dataset.sel.split(":");
  const sel = { type, id };
  const { project, clips } = data();
  const cur = NAMES[type] && current(project, clips, sel);
  if (!cur) return;
  const now = getState().editSel;
  if (!now || now.type !== type || now.id !== id) setState({ editSel: sel });
  if (type === "shot") focusShot(id, { tab: "edit" });
  openMenu(e, clipItems(data, sel, cur));
}
