// The settings of a timeline clip that are off what a new clip gets, and the edits that put the picked ones back
// (edit.resetAttrs, dialogs/reset-attrs.js). A row is one setting of one clip: {key, label, detail, op, item}
// with what resets it: patch (fields to write), unset (fields to drop), transform (fields of the shot's transform)
// and dropKeys (keyframe fields to remove); the picked rows of one clip become one op.
// Left out: when and how long the clip plays (trims, start, length, an upper clip's speed), its name, color and
// notes, the on/off switches, reverse and stabilize (other files of the shot), and a shot's sound keyframes and
// 壓低爆音 (kept on its file, shared with the clips split from it; the panel removes those).
import { NEUTRAL, CORNERS, SIDES, fullTransform, isIdentity } from "./transform.js";
import { isNeutral } from "./grade.js";
import { hasKeys } from "./keyframes.js";
import { fitFrame } from "./sequence.js";
import { hasOwnSound } from "./voice-stems.js";

const MOVE_KEYS = Object.keys(NEUTRAL).filter((k) => k !== "opacity");
const CUT = { type: "cut", duration: 0.5 };
const EPS = 0.00005;

const pct = (v) => `${Math.round(Number(v) * 100)}%`;
const keyCount = (item, fields) => (item.keyframes || []).filter((k) => fields.some((f) => f in k)).length;
const keyed = (n) => `關鍵幀 ${n} 個`;
const onOff = (v) => (v.on === false ? "已關閉" : "已開啟");
const keysWithout = (item, fields) => (item.keyframes || [])
  .map((k) => Object.fromEntries(Object.entries(k).filter(([f]) => !fields.includes(f))))
  .filter((k) => Object.keys(k).some((f) => f !== "t"));

// Volume, fades, 聲道, 降噪 and 去回音 of an audio clip, or of a shot's own sound (own).
function soundRows(item, own) {
  const name = own ? "原聲" : "";
  const id = (k) => (own ? `own.${k}` : k);
  const keys = keyCount(item, ["volume"]);
  const volume = Number(item.volume ?? 1);
  const { channel, denoise, dereverb } = item;
  return [
    (volume !== 1 || keys > 0) && { key: id("volume"), label: `${name}音量`, detail: keys ? keyed(keys) : pct(volume),
      patch: { volume: 1 }, dropKeys: ["volume"] },
    Number(item.fadeIn || 0) > 0 && { key: id("fadeIn"), label: `${name}淡入`, detail: `${Number(item.fadeIn)} 秒`, patch: { fadeIn: 0 } },
    Number(item.fadeOut || 0) > 0 && { key: id("fadeOut"), label: `${name}淡出`, detail: `${Number(item.fadeOut)} 秒`, patch: { fadeOut: 0 } },
    channel && { key: id("channel"), label: "聲道", detail: `只播聲道 ${Number(channel.index) + 1}`, patch: { channel: null } },
    denoise && { key: id("denoise"), label: "降噪", detail: onOff(denoise), unset: ["denoise"] },
    dereverb && { key: id("dereverb"), label: "去回音", detail: onOff(dereverb), unset: ["dereverb"] },
  ].filter(Boolean);
}

function shotRows(shot, clip) {
  const t = fullTransform(shot.transform);
  const masks = (shot.masks || []).length;
  const speed = Number(shot.speed || 1);
  const transition = shot.transition && shot.transition.type !== "cut" ? shot.transition : null;
  const sound = !shot.audioDetached && (!clip || hasOwnSound(clip));
  return [
    t.opacity < 1 && { key: "opacity", label: "不透明度", detail: pct(t.opacity), transform: { opacity: 1 } },
    MOVE_KEYS.some((k) => t[k] !== NEUTRAL[k]) && { key: "move", label: "變換", detail: "位置、旋轉、縮放、錨點",
      transform: Object.fromEntries(MOVE_KEYS.map((k) => [k, NEUTRAL[k]])) },
    SIDES.some((k) => t.crop[k]) && { key: "crop", label: "裁剪", detail: "", transform: { crop: { l: 0, r: 0, t: 0, b: 0 } } },
    CORNERS.some((k) => t.distort[k][0] || t.distort[k][1]) && { key: "distort", label: "變形", detail: "",
      transform: { distort: Object.fromEntries(CORNERS.map((k) => [k, [0, 0]])) } },
    !isNeutral(shot.grade) && { key: "grade", label: "調色", detail: "", patch: { grade: null } },
    masks > 0 && { key: "masks", label: "遮罩", detail: `${masks} 個`, patch: { masks: [] } },
    // the speed of a shot whose sound is detached is locked, as in the panel
    speed !== 1 && !shot.audioDetached && { key: "speed", label: "速度", detail: `${speed}x`, patch: { speed: 1 } },
    ...(sound ? soundRows(shot, true) : []),
    transition && { key: "transition", label: "轉場", detail: `${Number(transition.duration ?? CUT.duration)} 秒`,
      patch: { transition: CUT } },
  ].filter(Boolean);
}

// A new upper clip fills the frame (fitFrame) at full opacity and volume, without 去背.
function overlayRows(project, item) {
  const media = (project.media || {})[item.mediaId];
  const fit = media ? fitFrame(media, Number(project.settings.width), Number(project.settings.height)) : null;
  const placeKeys = keyCount(item, ["x", "y", "w"]);
  const opacityKeys = keyCount(item, ["opacity"]);
  const volumeKeys = keyCount(item, ["volume"]);
  const opacity = Number(item.opacity ?? 1);
  const volume = Number(item.volume ?? 1);
  return [
    fit && (placeKeys > 0 || Object.keys(fit).some((k) => Math.abs(Number(item[k]) - fit[k]) > EPS)) && {
      key: "place", label: "位置與大小", detail: placeKeys ? keyed(placeKeys) : "恢復成滿版", patch: fit, dropKeys: ["x", "y", "w"] },
    (opacity !== 1 || opacityKeys > 0) && { key: "opacity", label: "不透明度", detail: opacityKeys ? keyed(opacityKeys) : pct(opacity),
      patch: { opacity: 1 }, dropKeys: ["opacity"] },
    (volume !== 1 || volumeKeys > 0) && { key: "volume", label: "音量", detail: volumeKeys ? keyed(volumeKeys) : pct(volume),
      patch: { volume: 1 }, dropKeys: ["volume"] },
    item.chroma && { key: "chroma", label: "去背", detail: onOff(item.chroma), unset: ["chroma"] },
  ].filter(Boolean);
}

function adjustRows(item) {
  const keys = keyCount(item, ["grade"]);
  return !isNeutral(item.grade) || keys > 0
    ? [{ key: "grade", label: "調色", detail: keys ? keyed(keys) : "", patch: { grade: null }, dropKeys: ["grade"] }] : [];
}

const captionRows = (item) => (item.x != null || item.y != null
  ? [{ key: "place", label: "字幕位置", detail: "拖曳過，恢復成樣式的位置", patch: { x: null, y: null } }] : []);

// The rows of one clip. A shot whose sound is detached lists that audio clip's sound settings with its own.
export function adjusted(project, clips, type, item) {
  const of = (op, x, rows) => rows.map((r) => ({ ...r, op, item: x }));
  if (type === "audio") return of("audio.update", item, soundRows(item, false));
  if (type === "overlay") return of("overlay.update", item, overlayRows(project, item));
  if (type === "adjust") return of("adjust.update", item, adjustRows(item));
  if (type === "caption") return of("caption.update", item, captionRows(item));
  if (type !== "shot") return [];
  const audio = item.audioDetached ? (project.audio || []).find((a) => a.shotId === item.id) : null;
  return [...of("shot.update", item, shotRows(item, clips.find((c) => c.shot.id === item.id) || null)),
    ...(audio ? of("audio.update", audio, soundRows(audio, false)) : [])];
}

// Rows of several clips, one entry per setting: [{key, label, detail, rows}] in the order they were found.
export function grouped(rows) {
  const out = new Map();
  for (const r of rows) out.set(r.key, [...(out.get(r.key) || []), r]);
  return [...out.entries()].map(([key, list]) => ({ key, label: list[0].label, rows: list,
    detail: list.length > 1 ? `${list.length} 個片段` : list[0].detail }));
}

// One op per clip that puts every picked row of it back.
export function resetOps(rows) {
  const clips = new Map();
  for (const r of rows) clips.set(`${r.op}:${r.item.id}`, [...(clips.get(`${r.op}:${r.item.id}`) || []), r]);
  return [...clips.values()].map((list) => {
    const { op, item } = list[0];
    const parts = list.filter((r) => r.transform).map((r) => r.transform);
    const moved = parts.length ? Object.assign(fullTransform(item.transform), ...parts) : null;
    const drop = list.flatMap((r) => r.dropKeys || []);
    const unset = list.flatMap((r) => r.unset || []);
    const patch = {
      ...Object.assign({}, ...list.map((r) => r.patch || {})),
      ...(moved ? { transform: isIdentity(moved) ? null : moved } : {}),
      ...(drop.some((f) => hasKeys(item, f)) ? { keyframes: keysWithout(item, drop) } : {}),
    };
    return { op, id: item.id, patch, ...(unset.length ? { unset } : {}) };
  });
}
