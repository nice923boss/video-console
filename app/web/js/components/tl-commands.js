// Timeline commands on keys (lib/keymap.js "edit.*") beyond the playhead edits in tl-edit.js: enable or
// disable, volume, the I/O range, markers, freeze frames, detached sound, reverse, speed and keyframes.
// Each acts on the selected item, or with nothing selected on the picture clip under the playhead.
import { ops, focusShot } from "../lib/actions.js";
import { getState, setState } from "../lib/store.js";
import { clock, setTime, setPlaying, onClock } from "../lib/clock.js";
import { newId, round, clamp, fmtTime } from "../lib/format.js";
import { clipIndexAt, audioLength, audioMedia, videoTake } from "../lib/sequence.js";
import { valueAt, hasKeys, withKey } from "../lib/keyframes.js";
import { fullGrade } from "../lib/grade.js";
import { generate } from "../lib/jobs-actions.js";
import { keyHint } from "../lib/keymap.js";
import { toast } from "./toast.js";
import { splitOps } from "./tl-edit.js";

const LISTS = { audio: "audio", overlay: "overlays", caption: "captions", adjust: "adjustments" };
const OFF_FIELD = { shot: "disabled", overlay: "disabled", caption: "disabled", audio: "muted", adjust: "hidden" };
const VOLUME_MAX = 4;
const RAMP = 0.1; // seconds a range's volume takes to rise or fall to its level
const FREEZE_LEN = 2;
const EDGE = 0.1;
export const KEYABLE = ["overlay", "audio", "adjust"];

export const MARKER_COLORS = [
  { value: "blue", label: "藍色", hex: "#6ea8ff" }, { value: "red", label: "紅色", hex: "#ff6b6b" },
  { value: "orange", label: "橘色", hex: "#f5b14c" }, { value: "yellow", label: "黃色", hex: "#f2d94e" },
  { value: "green", label: "綠色", hex: "#4fd18b" }, { value: "purple", label: "紫色", hex: "#b28cff" },
];

const warn = (text) => toast(text, { level: "warn" });
const isStill = (project, item) => ((project.media || {})[item.mediaId] || {}).kind === "image";

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
// through volume keyframes (ramping over RAMP seconds at each end); otherwise the whole clip does.
export function nudgeVolume(project, clips, sel, dir, range) {
  const cur = current(project, clips, sel);
  if (!cur) return warn("先選取音訊或上方畫面的片段");
  if (cur.type === "shot") return warn(`鏡頭的原聲要先分離成音訊片段${keyHint("edit.detachAudio")}，才能調整音量`);
  if (cur.type !== "audio" && cur.type !== "overlay") return warn("這種片段沒有聲音");
  if (cur.type === "overlay" && isStill(project, cur.item)) return warn("圖片沒有聲音");
  const { item, length } = cur;
  const f = 10 ** (dir / 20);
  const scale = (v) => round(clamp(Math.max(Number(v), 0.001) * f, 0, VOLUME_MAX), 4);
  const base = (t) => Number(valueAt(item, "volume", t, 1));
  const a = range ? clamp(range.in - cur.start, 0, length) : 0;
  const b = range ? clamp(range.out - cur.start, 0, length) : length;
  if (b - a < 0.01) return warn("區域沒有蓋到選取的片段");
  const partial = a > 1e-3 || b < length - 1e-3;
  let patch;
  let shown;
  if (partial) {
    let keys = (item.keyframes || []).map((k) => ("volume" in k && k.t >= a - 1e-3 && k.t <= b + 1e-3 ? { ...k, volume: scale(k.volume) } : k));
    const put = (t, v) => {
      keys = withKey({ keyframes: keys }, t, { volume: v });
    };
    if (a > 1e-3) put(Math.max(0, a - RAMP), base(Math.max(0, a - RAMP)));
    if (b < length - 1e-3) put(Math.min(length, b + RAMP), base(Math.min(length, b + RAMP)));
    shown = scale(base(a));
    put(a, shown);
    put(b, scale(base(b)));
    patch = { keyframes: keys };
  } else if (hasKeys(item, "volume")) {
    patch = { keyframes: item.keyframes.map((k) => ("volume" in k ? { ...k, volume: scale(k.volume) } : k)) };
    shown = scale(base(clamp(clock.time - cur.start, 0, length)));
  } else {
    shown = scale(item.volume ?? 1);
    patch = { volume: shown };
  }
  ops([{ op: `${cur.type}.update`, id: item.id, patch }], { label: dir > 0 ? "調高音量" : "調低音量", coalesce: `${item.id}:nudge-volume` });
  toast(`${partial ? "區域內音量" : "音量"} ${fmtDb(shown)}`, { id: "tl-volume", duration: 1500 });
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

// Plays from the in point and stops at the out point; pausing or playing backwards drops the stop.
export function playRange() {
  const r = getState().editRange;
  if (!r) return warn(`先設定區域的入點${keyHint("edit.markIn")}與出點${keyHint("edit.markOut")}`);
  if (stopRange) stopRange();
  setPlaying(false);
  setTime(r.in);
  setPlaying(true);
  const off = onClock((c) => {
    if (c.playing && c.rate > 0 && c.time < r.out) return;
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
// Splits the video clip under the playhead there and puts a FREEZE_LEN still of that frame in between
// (before or after the clip when the playhead is at an edge). The still is filled in by a lastframe job.
export function freezeFrame(project, clips) {
  const clip = clips[clipIndexAt(clips, clock.time)];
  if (!clip || clip.source !== "video") return warn("播放頭下方要是影片片段才能定格");
  if (clip.shot.audioDetached) return warn(`已分離原聲的鏡頭要先還原原聲${keyHint("edit.detachAudio")}，才能定格`);
  const local = clock.time - clip.start;
  const at = round(Math.min(clip.out - 0.04, clip.in + local * clip.speed), 3);
  const id = newId("s");
  const still = { op: "shot.add", shot: { id, name: "定格", duration: FREEZE_LEN }, index: clip.index + 1 };
  let list;
  if (local < EDGE) list = [{ ...still, index: clip.index }];
  else if (clip.length - local < EDGE) list = [still];
  else list = [...splitOps({ type: "shot", clip, t: round(clock.time, 3), local }), still];
  ops(list, { label: "定格" }).then((res) => {
    if (res) generate("lastframe", [clip.shot.id], { payload: { source: clip.take.id, target: "shot", targetShot: id, at } });
  });
}

// Detach: the shot's sound becomes an audio clip under it (takeId + shotId) and the shot itself plays
// silent. Again on the shot or that audio: the audio clips go and the shot plays its own sound again.
// Shots cut from the same footage restore together, since a cut copies the detached flag to both halves.
export function detachAudio(project, clips, sel) {
  const cur = current(project, clips, sel);
  const audio = project.audio || [];
  const shotId = cur && (cur.type === "audio" ? cur.item.shotId : cur.type === "shot" && cur.item.audioDetached ? cur.item.id : null);
  if (shotId) {
    const takes = new Set(audio.filter((a) => a.shotId === shotId).map((a) => a.takeId));
    const items = audio.filter((a) => a.shotId && (a.shotId === shotId || takes.has(a.takeId)));
    const shots = project.shots.filter((s) => s.audioDetached && (s.id === shotId || takes.has(s.activeVideo)));
    ops([...items.map((a) => ({ op: "audio.delete", id: a.id })),
      ...shots.map((s) => ({ op: "shot.update", id: s.id, patch: { audioDetached: false } }))], { label: "還原原聲" });
    return;
  }
  const clip = cur && cur.type === "shot" ? cur.clip : null;
  if (!clip) return warn("先選取畫面軌上的鏡頭，或把播放頭移到鏡頭上");
  if (clip.source !== "video" || !clip.take.hasAudio) return warn("這個鏡頭沒有聲音");
  if (clip.speed !== 1) return warn("變速鏡頭不能分離音訊，先把速度改回 1x");
  const id = newId("a");
  const item = { id, takeId: clip.take.id, shotId: clip.shot.id, start: round(clip.start, 3), in: round(clip.in, 3),
    out: round(clip.out, 3), name: `${clip.shot.name || "鏡頭"} 原聲` };
  ops([{ op: "audio.add", item }, { op: "shot.update", id: clip.shot.id, patch: { audioDetached: true } }], { label: "分離原聲" })
    .then((res) => {
      if (res) setState({ editSel: { type: "audio", id } });
    });
}

export const isReverse = (take) => !!(take && take.source && take.source.tool === "reverse");

// The same kept footage once the clip runs the other way (mirrors localjobs.py _finish_reverse).
const mirrorTrims = (shot, dur) => ({
  trimIn: shot.trimOut != null ? round(Math.max(0, dur - shot.trimOut), 3) : 0,
  trimOut: (shot.trimIn || 0) > 0 ? round(dur - shot.trimIn, 3) : null,
});

// A reversed copy is rendered once (a "reverse" job); after that the key switches between the two.
export function reverseClip(project, clips, sel) {
  const cur = currentShot(project, clips, sel);
  const shot = cur && cur.type === "shot" ? cur.item : null;
  const take = shot && videoTake(project, shot);
  if (!take) return warn("倒放只能用在影片鏡頭");
  if (shot.audioDetached) return warn(`已分離原聲的鏡頭要先還原原聲${keyHint("edit.detachAudio")}，才能倒放`);
  const takes = project.takes || {};
  const other = isReverse(take) ? takes[take.parent]
    : Object.values(takes).find((t) => t.parent === take.id && isReverse(t) && t.kind === "video" && t.duration);
  if (other) {
    ops([{ op: "shot.update", id: shot.id, patch: { activeVideo: other.id, ...mirrorTrims(shot, Number(take.duration)) } }],
      { label: isReverse(take) ? "取消倒放" : "倒放" });
    return;
  }
  if (isReverse(take)) return warn("找不到倒放前的原始影片");
  generate("reverse", [shot.id], { payload: { source: take.id } });
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
  const { item } = cur;
  const local = clamp(t - cur.start, 0, cur.length);
  let values;
  if (cur.type === "adjust") {
    values = { grade: fullGrade(valueAt(item, "grade", local)) };
  } else {
    const fields = cur.type === "audio" ? { volume: 1 }
      : { x: 0, y: 0, w: 0.35, opacity: 1, ...(isStill(project, item) ? {} : { volume: 1 }) };
    values = Object.fromEntries(Object.entries(fields).map(([f, d]) => [f, round(Number(valueAt(item, f, local, d)), 4)]));
  }
  ops([{ op: `${cur.type}.update`, id: item.id, patch: { keyframes: withKey(item, local, values) } }], { label: "加入關鍵幀" });
  toast(`已在 ${fmtTime(cur.start + local)} 加入關鍵幀`, { id: "tl-key", duration: 1500 });
}

export function toggleKeyPanel(sel) {
  if (!sel || !KEYABLE.includes(sel.type)) return warn("先選取上方畫面、音訊或調整圖層的片段");
  setState({ keyPanel: !getState().keyPanel });
}

// Keys from the shortcut table that have no feature behind them yet (GAPS.md).
export const notYet = (what) => toast(`「${what}」還沒有支援`, { level: "warn" });
