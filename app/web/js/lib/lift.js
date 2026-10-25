// Lift (picture lane to an upper track): the shot's piece of its file becomes a clip on the upper track, and an
// empty shot of the same length and transition takes its place, so nothing after it moves.
import { newId, round } from "./format.js";
import { fitFrame, fillFrame } from "./sequence.js";

// Mirror of new_media in app/vc/takes.py: the fields a library item copies from a file.
const MEDIA_KEYS = ["kind", "file", "thumb", "strip", "width", "height", "duration", "fps", "hasAudio"];

// target: "layer:<id>" or "new-layer". Returns { ops, id } or { error } for shots an upper clip can't carry.
export function liftOps(project, clip, target, start) {
  const { shot, take } = clip;
  if (!take) return { error: "這個鏡頭還沒有畫面，沒有東西可以移到上層軌" };
  if (clip.speed !== 1) return { error: "變速的鏡頭要先改回原速，才能移到上層軌" };
  if (shot.audioDetached) return { error: "已分離原聲的鏡頭要先還原原聲，才能移到上層軌" };
  const list = [];
  let media = Object.values(project.media || {}).find((m) => m.file === take.file && m.kind === take.kind);
  if (!media) {
    media = {
      id: newId("m"), name: take.label || shot.name || "", created: Math.round(Date.now()) / 1000, favorite: false, note: "",
      ...Object.fromEntries(MEDIA_KEYS.map((k) => [k, take[k] ?? null])),
      ...(take.wave ? { wave: take.wave } : {}),
      ...(take.alphaCodec ? { alphaCodec: take.alphaCodec } : {}),
    };
    list.push({ op: "media.add", media });
  }
  let layer = target.slice(6);
  if (target === "new-layer") {
    layer = newId("l");
    list.push({ op: "layer.add", layer: { id: layer } });
  }
  const { width, height, fit } = project.settings;
  const place = (fit === "cover" ? fillFrame : fitFrame)(media, Number(width), Number(height));
  const id = newId("o");
  const item = {
    id, mediaId: media.id, layer, name: shot.name || media.name || "",
    start: round(Math.max(0, start), 3), in: clip.source === "video" ? round(clip.in, 3) : 0, length: round(clip.length, 3),
    ...place, opacity: 1, volume: 1, ...(shot.disabled ? { disabled: true } : {}),
  };
  const gap = { id: newId("s"), name: "空白", duration: round(Math.max(0.2, clip.length), 3), transition: shot.transition };
  list.push({ op: "overlay.add", item }, { op: "shot.delete", id: shot.id }, { op: "shot.add", shot: gap, index: clip.index });
  return { ops: list, id };
}
