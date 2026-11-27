// Copy and paste of timeline clips (edit.copy / edit.paste): shots, upper-track pictures, captions (with their
// voice-overs), adjustment layers and soundtrack clips. The copy lands at the playhead. And of a clip's settings
// (edit.copyAttrs / edit.pasteAttrs): a shot's picture and sound, an audio clip's sound.
import { ops } from "../lib/actions.js";
import { setState } from "../lib/store.js";
import { clock } from "../lib/clock.js";
import { newId, round } from "../lib/format.js";
import { clipIndexAt, audioMedia } from "../lib/sequence.js";
import { keyHint } from "../lib/keymap.js";
import { toast } from "./toast.js";
import { hasOwnSound } from "../lib/voice-stems.js";
import { current, together } from "./tl-commands.js";

const LISTS = { audio: "audio", overlay: "overlays", caption: "captions", adjust: "adjustments" };
const NAMES = { shot: "鏡頭", audio: "音訊片段", overlay: "上方畫面", caption: "字幕", adjust: "調整圖層" };
const warn = (text) => toast(text, { level: "warn" });
const clone = (x) => JSON.parse(JSON.stringify(x));

let copied = null; // { type, item, voices }: the clip as it was when copied

// Nothing selected: false lets the browser copy text as usual.
export function copyItem(project, clips, sel) {
  if (!sel) return false;
  const item = sel.type === "shot" ? project.shots.find((s) => s.id === sel.id)
    : (project[LISTS[sel.type]] || []).find((x) => x.id === sel.id);
  if (!item) return warn("找不到選取的片段");
  const voices = sel.type === "caption" ? (project.audio || []).filter((a) => a.captionId === item.id) : [];
  copied = clone({ type: sel.type, item, voices });
  toast(`已複製${NAMES[sel.type]}`, { id: "tl-copy", duration: 1500 });
}

export function pasteItem(project, clips) {
  if (!copied) return warn(`還沒有複製片段，先選取片段再按${keyHint("edit.copy")}`);
  const t = round(clock.time, 3);
  const paste = { shot: pasteShot, audio: pasteAudio, overlay: pasteOverlay, caption: pasteCaption, adjust: pasteAdjust };
  return paste[copied.type](project, clips, t, copied);
}

const done = (type, id) => (res) => {
  if (res) setState({ editSel: { type, id } });
  return res;
};

// After the clip under the playhead.
function pasteShot(project, clips, t, { item }) {
  const { parked, ...rest } = item; // what the original holds stays with it
  const k = clipIndexAt(clips, t);
  const index = k >= 0 ? clips[k].index + 1 : project.shots.length;
  const shot = { ...rest, id: newId("s"), name: item.name ? `${item.name} 副本` : "" };
  return ops([{ op: "shot.add", shot, index }], { label: "貼上鏡頭" }).then(done("shot", shot.id));
}

// On the same track while it is free there, otherwise on a new track above it.
function pasteOverlay(project, clips, t, { item }) {
  if (!project.media[item.mediaId]) return warn("複製的畫面素材不在這個專案裡");
  const layers = project.layers || [];
  const length = Number(item.length || 0);
  const busy = (project.overlays || []).some((o) => o.layer === item.layer && o.start < t + length && t < o.start + Number(o.length || 0));
  const list = [];
  let layer = item.layer;
  if (busy || !layers.some((l) => l.id === layer)) {
    layer = newId("l");
    const above = layers.findIndex((l) => l.id === item.layer);
    list.push({ op: "layer.add", layer: { id: layer }, ...(above >= 0 ? { index: above + 1 } : {}) });
  }
  const id = newId("o");
  list.push({ op: "overlay.add", item: { ...item, id, layer, start: t } });
  return ops(list, { label: "貼上上方畫面" }).then(done("overlay", id));
}

function pasteCaption(project, clips, t, { item, voices }) {
  const { shot, ...rest } = item; // the shot's own caption stays one per shot
  const id = newId("c");
  const list = [{ op: "caption.add", item: { ...rest, id, start: t, end: round(t + Number(item.end) - Number(item.start), 3) } }];
  for (const vo of voices.filter((a) => audioMedia(project, a))) {
    const start = round(t + Number(vo.start) - Number(item.start), 3);
    list.push({ op: "audio.add", item: { ...vo, id: newId("a"), captionId: id, start } });
  }
  return ops(list, { label: "貼上字幕" }).then(done("caption", id));
}

function pasteAdjust(project, clips, t, { item }) {
  const id = newId("adj");
  return ops([{ op: "adjust.add", item: { ...item, id, start: t } }], { label: "貼上調整圖層" }).then(done("adjust", id));
}

// The copy starts at the playhead on a track of its own (a clip without `track` is its own track). It keeps its
// caption or shot only while no other clip holds that slot (copy, delete, paste puts it back); otherwise it would
// take the caption's voice or the shot's sound away from the original, so it pastes as a plain clip.
function pasteAudio(project, clips, t, { item }) {
  if (!audioMedia(project, item)) return warn("複製的音訊素材不在這個專案裡");
  const { captionId, shotId, track, ...rest } = item;
  const audio = project.audio || [];
  const bind = {};
  if (captionId && (project.captions || []).some((c) => c.id === captionId) && !audio.some((a) => a.captionId === captionId)) {
    bind.captionId = captionId;
  }
  if (shotId && project.shots.some((s) => s.id === shotId && s.audioDetached) && !audio.some((a) => a.shotId === shotId)) {
    bind.shotId = shotId;
  }
  const id = newId("a");
  return ops([{ op: "audio.add", item: { ...rest, ...bind, id, start: t } }], { label: "貼上音訊" }).then((res) => {
    if (!res) return;
    setState({ editSel: { type: "audio", id } });
    if ((captionId && !bind.captionId) || (shotId && !bind.shotId)) {
      toast(`原片段還在，複本不綁定${captionId ? "字幕" : "鏡頭"}`, { id: "tl-copy", duration: 2500 });
    }
  });
}

// ---- a clip's settings: a shot's picture (不透明度, 變換, 裁剪, 變形, 調色, 遮罩, 綠幕去背 with its backdrop)
// and sound (volume, fades, 聲道, 降噪, 去回音, 壓縮器), an audio clip's sound

let attrs = null; // { look: {transform, grade, masks, chroma, backdrop} | null, sound: {...} | null }

const volumeKeyed = (item) => (item.keyframes || []).some((k) => k.volume != null);

// Where a clip's sound settings are kept: {op, item, file, keyed} of an audio clip, or of a shot's own sound (of
// its audio clip while detached). Null for a clip without sound.
function soundOf(project, clips, type, item) {
  if (type === "shot" && item.audioDetached) {
    const audio = (project.audio || []).find((a) => a.shotId === item.id);
    return audio ? soundOf(project, clips, "audio", audio) : null;
  }
  if (type === "audio") {
    const media = audioMedia(project, item);
    return media ? { op: "audio.update", item, file: media.file, keyed: volumeKeyed(item) } : null;
  }
  const clip = type === "shot" && clips.find((c) => c.shot.id === item.id);
  return clip && hasOwnSound(clip) ? { op: "shot.update", item, file: clip.take.file, keyed: false } : null;
}

const lookOf = ({ transform, grade, masks, chroma, backdrop }) => clone({ transform: transform || null,
  grade: grade || null, masks: masks || [], chroma: chroma || null, backdrop: backdrop || null });

const soundAttrs = (src) => clone({ volume: src.keyed ? null : Number(src.item.volume ?? 1),
  fadeIn: Number(src.item.fadeIn || 0), fadeOut: Number(src.item.fadeOut || 0), channel: src.item.channel || null,
  denoise: src.item.denoise || null, dereverb: src.item.dereverb || null, comp: src.item.comp || null, file: src.file });

// The whole set replaces the target's: a key or effect the source lacks is switched off, its settings kept.
function lookPatch(look, to) {
  const patch = { transform: look.transform, grade: look.grade, masks: look.masks, backdrop: look.backdrop };
  if (look.chroma) patch.chroma = look.chroma;
  else if (to.chroma) patch.chroma = { ...to.chroma, on: false };
  return clone(patch);
}

// The noise sample is a stretch of the source's file, so it goes along only onto clips of that file.
function soundPatch(sound, to) {
  const patch = { fadeIn: sound.fadeIn, fadeOut: sound.fadeOut, channel: sound.channel };
  if (sound.volume != null && !to.keyed) patch.volume = sound.volume;
  for (const key of ["denoise", "dereverb", "comp"]) {
    if (sound[key]) patch[key] = key === "denoise" && to.file !== sound.file ? { ...sound.denoise, sample: null } : sound[key];
    else if (to.item[key]) patch[key] = { ...to.item[key], on: false };
  }
  return patch;
}

const what = (look, sound) => (look && sound ? "畫面與聲音屬性" : look ? "畫面屬性" : "聲音屬性");

// The selected shot or audio clip, else the shot under the playhead. Keyframes stay with their clip, so a volume
// set by keyframes is not copied.
export function copyAttrs(project, clips, sel) {
  const cur = current(project, clips, sel);
  if (!cur || (cur.type !== "shot" && cur.type !== "audio")) return warn("先選取鏡頭或音訊片段，再複製屬性");
  const src = soundOf(project, clips, cur.type, cur.item);
  const look = cur.type === "shot" ? lookOf(cur.item) : null;
  if (!look && !src) return warn("這段音訊找不到來源檔，沒有屬性可複製");
  attrs = { look, sound: src && soundAttrs(src) };
  toast(`已複製${what(look, src)}${src && src.keyed ? "（音量有關鍵幀，不含音量）" : ""}`, { id: "tl-copy", duration: 1500 });
}

// Onto every selected shot or audio clip, one undo step: the picture onto shots, the sound onto whatever has sound.
export function pasteAttrs(project, clips, sel) {
  if (!attrs) return warn(`還沒有複製屬性，先選取鏡頭或音訊片段再按${keyHint("edit.copyAttrs")}`);
  const cur = current(project, clips, sel);
  if (!cur || (cur.type !== "shot" && cur.type !== "audio")) return warn("屬性只能貼到鏡頭或音訊片段");
  const targets = together(project, sel, cur.item);
  const { look, sound } = attrs;
  const patches = new Map(); // "op id" -> {op, id, patch}: a shot's picture and its own sound share one update
  const add = (op, id, patch) => {
    const key = `${op} ${id}`;
    patches.set(key, { op, id, patch: { ...(patches.get(key)?.patch || {}), ...patch } });
  };
  let looked = 0;
  const sounds = [];
  for (const to of targets) {
    if (look && cur.type === "shot") {
      add("shot.update", to.id, lookPatch(look, to));
      looked += 1;
    }
    const dest = sound && soundOf(project, clips, cur.type, to);
    if (dest) {
      add(dest.op, dest.item.id, soundPatch(sound, dest));
      sounds.push(dest);
    }
  }
  if (!patches.size) {
    return warn(cur.type === "audio" ? "複製的是畫面屬性，只能貼到鏡頭" : "複製的是聲音屬性，只能貼到音訊片段或有原聲的鏡頭");
  }
  const other = sound && sound.denoise && sound.denoise.sample ? sounds.filter((to) => to.file !== sound.file).length : 0;
  const keyed = sound && sound.volume != null ? sounds.filter((to) => to.keyed).length : 0;
  const count = Math.max(looked, sounds.length);
  return ops([...patches.values()], { label: `貼上${what(looked, sounds.length)}` }).then((res) => {
    if (!res) return res;
    const notes = [other && `${other} 段的來源檔不同，降噪要重新取樣雜訊`, keyed && `${keyed} 段有音量關鍵幀，音量不變`].filter(Boolean);
    toast(`已貼上${what(looked, sounds.length)}${count > 1 ? `到 ${count} 個片段` : ""}${notes.length ? `；${notes.join("；")}` : ""}`,
      { id: "tl-copy", level: other ? "warn" : undefined, duration: notes.length ? 5000 : 1500 });
    return res;
  });
}
