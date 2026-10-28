// Timeline commands on keys (lib/keymap.js "edit.*") beyond the playhead edits in tl-edit.js: enable or
// disable, volume, the I/O range, markers, freeze frames, detached sound, reverse, speed and keyframes.
// Each acts on the selected item, or with nothing selected on the picture clip under the playhead. The
// server works out the volume, freeze, detach, reverse and keyframe edits (POST /edit, vc/edits.py), the same
// ones Claude's edits get; it also refuses what cannot be done there (an image has no sound...).
import { ops, editCmd, focusShot } from "../lib/actions.js";
import { getState, setState } from "../lib/store.js";
import { clock, setTime, setPlaying, onClock } from "../lib/clock.js";
import { newId, round, clamp, fmtTime } from "../lib/format.js";
import { clipIndexAt, audioLength, audioMedia, videoTake } from "../lib/sequence.js";
import { showQueued } from "../lib/jobs-actions.js";
import { keyHint } from "../lib/keymap.js";
import { readPref } from "../lib/prefs.js";
import { toast } from "./toast.js";

const LISTS = { audio: "audio", overlay: "overlays", caption: "captions", adjust: "adjustments" };
const OFF_FIELD = { shot: "disabled", overlay: "disabled", caption: "disabled", audio: "muted", adjust: "hidden" };
export const KEYABLE = ["overlay", "audio", "adjust"];

export const MARKER_COLORS = [
  { value: "blue", label: "藍色", hex: "#6ea8ff" }, { value: "red", label: "紅色", hex: "#ff6b6b" },
  { value: "orange", label: "橘色", hex: "#f5b14c" }, { value: "yellow", label: "黃色", hex: "#f2d94e" },
  { value: "green", label: "綠色", hex: "#4fd18b" }, { value: "purple", label: "紫色", hex: "#b28cff" },
];

const warn = (text) => toast(text, { level: "warn" });

// What a command acts on: {type, item, start, length, clip}, or null.
export function current(project, clips, sel) {
  if (sel && sel.type !== "shot") {
    const item = (project[LISTS[sel.type]] || []).find((x) => x.id === sel.id);
    if (!item) return null;
    const start = Number(item.start || 0);
    const length = sel.type === "audio" ? audioLength(item, audioMedia(project, item))
      : sel.type === "caption" ? Number(item.end || 0) - start : Number(item.length || 0);
    return { type: sel.type, item, start, length, clip: null };
  }
  const clip = sel ? clips.find((c) => c.shot.id === sel.id) : clips[clipIndexAt(clips, clock.time)];
  if (clip) return { type: "shot", item: clip.shot, start: clip.start, length: clip.length, clip };
  const shot = sel && project.shots.find((s) => s.id === sel.id);
  return shot ? { type: "shot", item: shot, start: 0, length: 0, clip: null } : null;
}

// Picture commands (freeze, reverse, speed) use the selected shot, else the shot under the playhead.
const currentShot = (project, clips, sel) => current(project, clips, sel && sel.type === "shot" ? sel : null);

export function toggleEnabled(project, clips, sel) {
  const cur = current(project, clips, sel);
  if (!cur) return warn("播放頭下方沒有片段");
  const field = OFF_FIELD[cur.type];
  const off = !cur.item[field];
  ops([{ op: `${cur.type}.update`, id: cur.item.id, patch: { [field]: off } }], { label: off ? "停用片段" : "啟用片段" });
}

// ---------------------------------------------------------------- volume
const fmtDb = (v) => (v <= 0.0011 ? "靜音" : `${(20 * Math.log10(v)).toFixed(1)} dB`);

// dir +1 / -1 dB. With an I/O range that covers only part of the clip, the range gets louder or softer
// through volume keyframes (ramping in and out at each end); otherwise the whole clip does.
export function nudgeVolume(project, clips, sel, dir, range) {
  const cur = current(project, clips, sel);
  if (!cur) return warn("先選取音訊或上方畫面的片段");
  const span = range ? { in: range.in, out: range.out } : {};
  // Quick presses on the same span are one undo step. A whole-clip press can set the volume while a ranged one
  // sets keyframes, and a merged step only puts back what its first press changed, so each span keys its own.
  const scope = range ? `${range.in}-${range.out}` : "all";
  editCmd({ cmd: "volume", type: cur.type, id: cur.item.id, db: dir, ...span, t: clock.time },
    { coalesce: `${cur.item.id}:nudge-volume:${scope}` }).then((res) => {
    if (res) toast(`${res.partial ? "區域內音量" : "音量"} ${fmtDb(res.level)}`, { id: "tl-volume", duration: 1500 });
  });
}

// ---------------------------------------------------------------- I/O range
export function markIn(end) {
  const t = round(clock.time, 3);
  const old = getState().editRange;
  setState({ editRange: { in: t, out: old && old.out > t + 0.01 ? old.out : round(Math.max(end, t + 1), 3) } });
}

export function markOut() {
  const t = round(clock.time, 3);
  if (t < 0.01) return warn("出點要在入點後面");
  const old = getState().editRange;
  setState({ editRange: { in: old && old.in < t - 0.01 ? old.in : 0, out: t } });
}

export function rangeFromClip(project, clips, sel) {
  const cur = current(project, clips, sel);
  if (!cur || !(cur.length > 0)) return warn("先選取片段，或把播放頭移到片段上");
  setState({ editRange: { in: round(cur.start, 3), out: round(cur.start + cur.length, 3) } });
}

export const clearRange = () => setState({ editRange: null });

let stopRange = null;

// Plays from the in point and stops at the out point, or with the player's loop on (player.js
// "vc.player.loop") goes back to the in point; pausing or playing backwards drops the stop.
export function playRange() {
  const r = getState().editRange;
  if (!r) return warn(`先設定區域的入點${keyHint("edit.markIn")}與出點${keyHint("edit.markOut")}`);
  if (stopRange) stopRange();
  setPlaying(false);
  setTime(r.in);
  setPlaying(true);
  const off = onClock((c) => {
    if (c.playing && c.rate > 0 && c.time < r.out) return;
    if (c.playing && c.rate > 0 && readPref("vc.player.loop", false, (v) => typeof v === "boolean")) {
      setTime(r.in);
      return;
    }
    off();
    stopRange = null;
    if (c.playing && c.rate > 0) {
      setPlaying(false);
      setTime(r.out);
    }
  });
  stopRange = off;
}

// ---------------------------------------------------------------- markers
const markerAt = (project, t) => (project.markers || []).find((m) => Math.abs(Number(m.start) - t) < 0.05);

export function markerAdd(project) {
  const t = round(clock.time, 3);
  if (markerAt(project, t)) return warn("播放頭這裡已經有標記");
  ops([{ op: "marker.add", item: { id: newId("mk"), start: t, color: "blue", name: "" } }], { label: "新增標記" });
}

export function markerColor(project) {
  const m = markerAt(project, clock.time);
  if (!m) return warn(`播放頭這裡沒有標記，先按${keyHint("edit.markerAdd")}新增`);
  const i = MARKER_COLORS.findIndex((c) => c.value === m.color);
  const next = MARKER_COLORS[(i + 1) % MARKER_COLORS.length];
  ops([{ op: "marker.update", id: m.id, patch: { color: next.value } }], { label: "修改標記顏色" });
  toast(`標記改成${next.label}`, { id: "tl-marker", duration: 1500 });
}

export function markerJump(project, dir) {
  const starts = (project.markers || []).map((m) => Number(m.start)).sort((x, y) => x - y);
  const t = clock.time;
  const hit = dir > 0 ? starts.find((s) => s > t + 0.01) : [...starts].reverse().find((s) => s < t - 0.01);
  if (hit == null) return toast(dir > 0 ? "後面沒有標記" : "前面沒有標記", { id: "tl-marker", duration: 1500 });
  setTime(hit);
}

// ---------------------------------------------------------------- picture
// Splits the video clip under the playhead there and puts a still of that frame in between (before or
// after the clip when the playhead is at an edge). The still is filled in by a lastframe job.
export function freezeFrame() {
  editCmd({ cmd: "freeze", t: round(clock.time, 3) }).then((res) => res && res.job && showQueued(res.job));
}

// Detach: the shot's sound becomes an audio clip under it and the shot itself plays silent. Again on the
// shot or that audio: the audio clips go and the shot plays its own sound again (with the shots cut from
// the same footage).
export function detachAudio(project, clips, sel) {
  const cur = current(project, clips, sel);
  const detached = cur && (cur.type === "audio" ? cur.item.shotId : cur.type === "shot" && cur.item.audioDetached);
  if (detached) {
    editCmd({ cmd: "restore", id: cur.item.id });
    return;
  }
  const clip = cur && cur.type === "shot" ? cur.clip : null;
  if (!clip) return warn("先選取畫面軌上的鏡頭，或把播放頭移到鏡頭上");
  editCmd({ cmd: "detach", id: clip.shot.id }).then((res) => {
    if (res && res.id) setState({ editSel: { type: "audio", id: res.id } });
  });
}

export const isReverse = (take) => !!(take && take.source && take.source.tool === "reverse");

// A reversed copy is rendered once (a "reverse" job); after that the key switches between the two.
export function reverseClip(project, clips, sel) {
  const cur = currentShot(project, clips, sel);
  if (!cur || cur.type !== "shot") return warn("倒放只能用在影片鏡頭");
  editCmd({ cmd: "reverse", id: cur.item.id }).then((res) => res && res.job && showQueued(res.job));
}

// Opens the shot's speed setting in the inspector (SpeedSection in insp-edit.js).
export function focusSpeed(project, clips, sel) {
  const cur = currentShot(project, clips, sel);
  if (!cur || cur.type !== "shot" || !videoTake(project, cur.item)) return warn("變速只能用在影片鏡頭");
  setState({ editSel: { type: "shot", id: cur.item.id } });
  focusShot(cur.item.id, { tab: "edit" });
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const el = document.querySelector(".insp-speed select");
    if (el) el.focus();
  }));
}

// ---------------------------------------------------------------- keyframes
// A key at t (timeline seconds) holding what the clip shows and plays there. Shots and captions take
// none: put the picture on an upper track, or grade it with an adjustment layer.
export function addKeyframe(project, clips, sel, t = clock.time) {
  const cur = current(project, clips, sel);
  if (!cur || !KEYABLE.includes(cur.type)) return warn("先選取上方畫面、音訊或調整圖層的片段；鏡頭與字幕不能加關鍵幀");
  if (t < cur.start - 1e-3 || t > cur.start + cur.length + 1e-3) return warn("播放頭不在選取的片段上");
  editCmd({ cmd: "keyframe", type: cur.type, id: cur.item.id, t: round(t, 3) }).then((res) => {
    if (res) toast(`已在 ${fmtTime(clamp(t, cur.start, cur.start + cur.length))} 加入關鍵幀`, { id: "tl-key", duration: 1500 });
  });
}

export function toggleKeyPanel(sel) {
  if (!sel || !KEYABLE.includes(sel.type)) return warn("先選取上方畫面、音訊或調整圖層的片段");
  setState({ keyPanel: !getState().keyPanel });
}

// Keys from the shortcut table that have no feature behind them yet (GAPS.md).
export const notYet = (what) => toast(`「${what}」還沒有支援`, { level: "warn" });
