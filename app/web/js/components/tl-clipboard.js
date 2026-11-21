// Copy and paste of timeline clips (edit.copy / edit.paste): shots, upper-track pictures, captions (with their
// voice-overs), adjustment layers and soundtrack clips. The copy lands at the playhead. And of a sound's settings
// (edit.copyAttrs / edit.pasteAttrs), between audio clips and the shots' own sound.
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

// ---- a sound's settings: volume, fades, 聲道, 降噪 and 去回音

let attrs = null; // { volume (null: the source's volume is keyed), fadeIn, fadeOut, channel, denoise, dereverb, file }

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

// The selected audio clip or shot, else the shot under the playhead. Keyframes stay with their clip, so a volume
// set by keyframes is not copied.
export function copyAttrs(project, clips, sel) {
  const cur = current(project, clips, sel);
  const src = cur && soundOf(project, clips, cur.type, cur.item);
  if (!src) return warn("先選取音訊片段或有原聲的鏡頭，再複製聲音屬性");
  const { item } = src;
  attrs = clone({ volume: src.keyed ? null : Number(item.volume ?? 1), fadeIn: Number(item.fadeIn || 0),
    fadeOut: Number(item.fadeOut || 0), channel: item.channel || null, denoise: item.denoise || null,
    dereverb: item.dereverb || null, file: src.file });
  toast(`已複製聲音屬性${src.keyed ? "（音量有關鍵幀，不含音量）" : ""}`, { id: "tl-copy", duration: 1500 });
}

// Onto every selected audio clip or shot, one undo step. The noise sample is a stretch of the source's file, so it
// goes along only onto clips of that file; an effect the source lacks is switched off.
export function pasteAttrs(project, clips, sel) {
  if (!attrs) return warn(`還沒有複製聲音屬性，先選取音訊片段或鏡頭再按${keyHint("edit.copyAttrs")}`);
  const cur = current(project, clips, sel);
  const sounds = cur ? together(project, sel, cur.item).map((x) => soundOf(project, clips, cur.type, x)).filter(Boolean) : [];
  if (!sounds.length) return warn("聲音屬性只能貼到音訊片段或有原聲的鏡頭");
  const sampled = !!(attrs.denoise && attrs.denoise.sample);
  const list = sounds.map((to) => {
    const patch = { fadeIn: attrs.fadeIn, fadeOut: attrs.fadeOut, channel: attrs.channel };
    if (attrs.volume != null && !to.keyed) patch.volume = attrs.volume;
    if (attrs.denoise) patch.denoise = to.file === attrs.file ? attrs.denoise : { ...attrs.denoise, sample: null };
    else if (to.item.denoise) patch.denoise = { ...to.item.denoise, on: false };
    if (attrs.dereverb) patch.dereverb = attrs.dereverb;
    else if (to.item.dereverb) patch.dereverb = { ...to.item.dereverb, on: false };
    return { op: to.op, id: to.item.id, patch };
  });
  const other = sampled ? sounds.filter((to) => to.file !== attrs.file).length : 0;
  const keyed = attrs.volume != null ? sounds.filter((to) => to.keyed).length : 0;
  return ops(list, { label: "貼上聲音屬性" }).then((res) => {
    if (!res) return res;
    const notes = [other && `${other} 段的來源檔不同，降噪要重新取樣雜訊`, keyed && `${keyed} 段有音量關鍵幀，音量不變`].filter(Boolean);
    toast(`已貼上聲音屬性${sounds.length > 1 ? `到 ${sounds.length} 個片段` : ""}${notes.length ? `；${notes.join("；")}` : ""}`,
      { id: "tl-copy", level: other ? "warn" : undefined, duration: notes.length ? 5000 : 1500 });
    return res;
  });
}
