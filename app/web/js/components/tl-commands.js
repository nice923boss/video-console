// Timeline commands on keys (lib/keymap.js "edit.*") beyond the playhead edits in tl-edit.js: enable or
// disable, volume, the I/O range, markers, freeze frames, detached sound, reverse, speed and keyframes.
// Each acts on the selected item, or with nothing selected on the picture clip under the playhead. The
// server works out the volume, freeze, detach, reverse and keyframe edits (POST /edit, vc/edits.py), the same
// ones Claude's edits get; it also refuses what cannot be done there (an image has no sound...).
import { ops, editCmd, focusShot } from "../lib/actions.js";
import { getState, setState } from "../lib/store.js";
import { clock, setTime, setPlaying, onClock } from "../lib/clock.js";
import { newId, round, clamp, fmtTime } from "../lib/format.js";
import { clipIndexAt, clipSpeed, audioLength, audioMedia, videoTake } from "../lib/sequence.js";
import { slipRange } from "../lib/roll.js";
import { showQueued, upsertJobs } from "../lib/jobs-actions.js";
import { keyHint } from "../lib/keymap.js";
import { readPref } from "../lib/prefs.js";
import { toast } from "./toast.js";
import { multiAudio } from "./tl-audio-sel.js";
import { multiClip, CLIP_SEL_TYPES } from "./tl-clip-sel.js";

const LISTS = { audio: "audio", overlay: "overlays", caption: "captions", adjust: "adjustments" };
export const OFF_FIELD = { shot: "disabled", overlay: "disabled", caption: "disabled", audio: "muted", adjust: "hidden" };
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

// Picture commands (reverse, speed) use the selected shot, else the shot under the playhead.
const currentShot = (project, clips, sel) => current(project, clips, sel && sel.type === "shot" ? sel : null);

// The items Shift or Ctrl+clicked together with `sel` (shots, audio clips, upper-track clips or adjustment
// layers), else just `item`.
export function together(project, sel, item) {
  const s = getState();
  if (sel && sel.type === "shot" && s.selected.length > 1 && s.selected.includes(sel.id)) {
    return project.shots.filter((x) => s.selected.includes(x.id));
  }
  const audio = sel && sel.type === "audio" && multiAudio(s);
  if (audio && audio.includes(sel.id)) return (project.audio || []).filter((a) => audio.includes(a.id));
  const many = sel && CLIP_SEL_TYPES.includes(sel.type) && multiClip(s);
  if (many && many.includes(sel.id)) return (project[LISTS[sel.type]] || []).filter((x) => many.includes(x.id));
  return [item];
}

// With several clips selected, one press turns them all off when any is on, else all back on; one undo step.
export function toggleEnabled(project, clips, sel) {
  const cur = current(project, clips, sel);
  if (!cur) return warn("播放頭下方沒有片段");
  const field = OFF_FIELD[cur.type];
  const items = together(project, sel, cur.item);
  const off = items.some((x) => !x[field]);
  const what = items.length > 1 ? ` ${items.length} 個片段` : "片段";
  ops(items.map((x) => ({ op: `${cur.type}.update`, id: x.id, patch: { [field]: off } })),
    { label: `${off ? "停用" : "啟用"}${what}` });
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

// Moves the playhead to the next (dir 1) or previous marker; returns its time, or null when there is none.
export function markerJump(project, dir) {
  const starts = (project.markers || []).map((m) => Number(m.start)).sort((x, y) => x - y);
  const t = clock.time;
  const hit = dir > 0 ? starts.find((s) => s > t + 0.01) : [...starts].reverse().find((s) => s < t - 0.01);
  if (hit == null) {
    toast(dir > 0 ? "後面沒有標記" : "前面沒有標記", { id: "tl-marker", duration: 1500 });
    return null;
  }
  setTime(hit);
  return hit;
}

// ---------------------------------------------------------------- picture
// Splits the video clip under the playhead there and puts a still of that frame in between (before or
// after the clip when the playhead is at an edge). The still is filled in by a lastframe job. A selected
// upper-track clip gets its still on its own track instead, once the job has grabbed the picture.
export function freezeFrame(project, clips, sel) {
  const t = round(clock.time, 3);
  const over = sel && sel.type === "overlay" ? current(project, clips, sel) : null;
  if (over && (t < over.start - 1e-3 || t > over.start + over.length + 1e-3)) return warn("播放頭不在選取的片段上");
  editCmd({ cmd: "freeze", t, ...(over && { type: "overlay", id: over.item.id }) })
    .then((res) => res && res.job && showQueued(res.job));
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

export const isStabilized = (take) => !!(take && take.source && take.source.tool === "stabilize");

// A stabilized copy of the clip's range is rendered once (a "stabilize" job); after that the box switches between
// the two, until the clip is trimmed wider than the copy. type "overlay": an upper-track clip (its copy is a library item).
// settings (stab-panel.js STAB_DEFAULTS keys): make sure it is stabilized exactly that way, rendering anew if needed.
export function stabilizeClip(id, type = "shot", settings = null) {
  const extra = settings ? { on: true, settings } : {};
  editCmd({ cmd: "stabilize", id, type, ...extra }).then((res) => res && res.job && showQueued(res.job));
}

// Several shots in one undo step (the multi-select inspector): on stabilizes the video shots not yet stabilized,
// off switches the stabilized ones back; copies not made yet render in the background.
export function stabilizeShots(ids, on) {
  editCmd({ cmd: "stabilizeShots", ids, on }).then((res) => {
    if (!res) return;
    const jobs = res.jobs || [];
    upsertJobs(jobs);
    toast(jobs.length ? `${res.label}，${jobs.length} 個在背景處理` : res.label, { duration: 2400 });
  });
}

// Opens the speed setting in the inspector: a shot's (SpeedSection in insp-edit.js), or a selected upper-track
// video clip's (OverlayPanel in edit-props.js), or the one speed of a set of them picked together.
export function focusSpeed(project, clips, sel) {
  if (sel && sel.type === "overlay") {
    const cur = current(project, clips, sel);
    if (!cur) return;
    const many = multiClip(getState());
    const set = many && many.includes(cur.item.id) ? (project.overlays || []).filter((o) => many.includes(o.id)) : [cur.item];
    if (!set.some((o) => (project.media[o.mediaId] || {}).kind === "video")) return warn("變速只能用在影片片段（圖片沒有速度）");
    const pick = { type: "overlay", id: cur.item.id };
    setState(set.length > 1 ? { editSel: pick } : { editSel: pick, clipSel: null });
  } else {
    const cur = currentShot(project, clips, sel);
    if (!cur || cur.type !== "shot" || !videoTake(project, cur.item)) return warn("變速只能用在影片鏡頭");
    setState({ editSel: { type: "shot", id: cur.item.id } });
    focusShot(cur.item.id, { tab: "edit" });
  }
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const el = document.querySelector(".insp-speed select");
    if (el) el.focus();
  }));
}

// ---------------------------------------------------------------- slip by one frame
// Ctrl+Alt+Left/Right: the clip keeps its place and length while its in and out points move one frame of its
// footage earlier or later (dir -1 or 1). A shot goes through the trim tool's slip (POST /edit "slip", picture
// only: its own sound is detached and stays put); an upper-track video clip moves its `in`. Presses in a row
// make one undo step. The project here can lag the presses already sent, so an upper-track clip counts on from
// the last `in` it was given.
let slipping = null;
const frameRate = (project, fps) => (Number(fps) > 0 ? Number(fps) : Math.max(1, Number(project.settings.fps) || 24));
// The next frame start from `t` in direction `dir`: one frame on when `t` sits on a frame start (within a
// twentieth of a frame, since in points keep 3 decimals), else the nearest frame start that way.
const stepFrame = (t, fps, dir) => (dir > 0 ? Math.floor(t * fps + 0.05) + 1 : Math.ceil(t * fps - 0.05) - 1) / fps;

export function slipFrame(project, clips, sel, dir) {
  const cur = sel && sel.type === "overlay" ? current(project, clips, sel) : currentShot(project, clips, sel);
  if (!cur || (cur.type !== "shot" && cur.type !== "overlay")) return warn("滑動內容只能用在影片鏡頭或上方軌的影片片段");
  const edge = dir > 0 ? "已經是素材最後面的畫面" : "已經是素材最前面的畫面";
  if (cur.type === "overlay") {
    const item = cur.item;
    const media = project.media[item.mediaId] || {};
    if (media.kind !== "video") return warn("滑動內容只能用在影片片段（圖片沒有其他畫面）");
    const fps = frameRate(project, media.fps);
    const base = slipping && slipping.id === item.id && Date.now() - slipping.at < 1500 ? slipping.in : Number(item.in || 0);
    const hi = Math.max(0, Number(media.duration || 0) - cur.length * clipSpeed(item, "video"));
    const target = stepFrame(base, fps, dir);
    const next = dir > 0 ? Math.min(target, hi) : Math.max(target, 0);
    if (dir > 0 ? next <= base + 1e-3 : next >= base - 1e-3) return warn(edge);
    slipping = { id: item.id, in: next, at: Date.now() };
    ops([{ op: "overlay.update", id: item.id, patch: { in: round(next, 3) } }], { label: "滑動內容", coalesce: `${item.id}:slip-frame` });
    return;
  }
  const clip = cur.clip;
  if (!clip || clip.source !== "video") return warn("滑動內容只能用在影片鏡頭");
  const fps = frameRate(project, clip.take.fps);
  const by = clamp((stepFrame(clip.in, fps, dir) - clip.in) / clip.speed, ...slipRange(clip));
  if (Math.abs(by) < 1e-3) return warn(edge);
  editCmd({ cmd: "slip", id: clip.shot.id, by: round(by, 4) }, { coalesce: `${clip.shot.id}:slip-frame` });
}

// ---------------------------------------------------------------- keyframes
// A key at t (timeline seconds) holding what the clip shows and plays there. A selected shot takes one on its
// own sound only (the 原聲 volume); captions take none.
export function addKeyframe(project, clips, sel, t = clock.time) {
  const cur = sel ? current(project, clips, sel) : null;
  if (!cur || (!KEYABLE.includes(cur.type) && cur.type !== "shot")) return warn("先選取上方畫面、音訊、調整圖層或原聲的片段；字幕不能加關鍵幀");
  if (cur.type === "shot" && !(cur.clip && cur.clip.source === "video" && cur.clip.take.hasAudio && !cur.item.audioDetached)) {
    return warn("鏡頭只能在原聲上加音量關鍵幀；這個鏡頭沒有原聲或原聲已分離");
  }
  if (t < cur.start - 1e-3 || t > cur.start + cur.length + 1e-3) return warn("播放頭不在選取的片段上");
  editCmd({ cmd: "keyframe", type: cur.type, id: cur.item.id, t: round(t, 3) }).then((res) => {
    if (res) toast(`已在 ${fmtTime(clamp(t, cur.start, cur.start + cur.length))} 加入關鍵幀`, { id: "tl-key", duration: 1500 });
  });
}

export function toggleKeyPanel(sel) {
  if (!sel || (!KEYABLE.includes(sel.type) && sel.type !== "shot")) return warn("先選取上方畫面、音訊、調整圖層或原聲的片段");
  setState({ keyPanel: !getState().keyPanel });
}

// Keys from the shortcut table that have no feature behind them yet (GAPS.md).
export const notYet = (what) => toast(`「${what}」還沒有支援`, { level: "warn" });
