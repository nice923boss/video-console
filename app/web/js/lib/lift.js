// Lift (picture lane to an upper track): the shot's piece of its file becomes a clip on the upper track, and an
// empty shot of the same length and transition takes its place, so nothing after it moves. Drop is the way back.
import { clamp, newId, round } from "./format.js";
import { buildSequence, fitFrame, fillFrame } from "./sequence.js";

// Mirror of new_media in app/vc/takes.py: the fields a library item copies from a file.
const MEDIA_KEYS = ["kind", "file", "thumb", "strip", "width", "height", "duration", "fps", "hasAudio"];

// The one sound detached under the shot, still on the shot's piece of the file as a drop left it (dropOps);
// null once it was moved, trimmed, split or the shot has none.
function dropSound(project, clip) {
  const sounds = (project.audio || []).filter((a) => a.shotId === clip.shot.id);
  const near = (a, b) => Math.abs(Number(a) - Number(b)) < 1e-3;
  const [a] = sounds;
  return sounds.length === 1 && a.takeId === clip.take.id && near(a.start, clip.start) && near(a.in, clip.in)
    && near(a.out, clip.out) ? a : null;
}

// target: "layer:<id>" or "new-layer". Returns { ops, id } or { error } for shots an upper clip can't carry.
export function liftOps(project, clip, target, start) {
  const { shot, take } = clip;
  if (!take) return { error: "這個鏡頭還沒有畫面，沒有東西可以移到上層軌" };
  if (clip.speed !== 1) return { error: "變速的鏡頭要先改回原速，才能移到上層軌" };
  const sound = shot.audioDetached ? dropSound(project, clip) : null;
  if (shot.audioDetached && !sound) return { error: "已分離原聲的鏡頭要先還原原聲，才能移到上層軌" };
  const list = sound ? [{ op: "audio.delete", id: sound.id }] : [];
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
    ...place, opacity: 1, volume: 1, ...(shot.pip || {}), ...(sound ? { volume: Number(sound.volume ?? 1) } : {}),
    ...(shot.disabled ? { disabled: true } : {}),
  };
  const gap = { id: newId("s"), name: "空白", duration: round(Math.max(0.2, clip.length), 3), transition: shot.transition };
  list.push({ op: "overlay.add", item }, { op: "shot.delete", id: shot.id }, { op: "shot.add", shot: gap, index: clip.index });
  return { ops: list, id };
}

// Fields of an upper clip a shot keeps in "pip" (ED-16) for a lift back to restore.
const PIP_KEYS = ["x", "y", "w", "opacity", "volume", "chroma", "keyframes"];

// Drop (upper track to picture lane): the clip becomes a shot at `index` with the same piece of the file, and
// the shots after it move back by its length. The picture lane plays shots full frame, so the clip's place,
// size, opacity and keying wait in shot.pip; a volume other than 100% stays audible on the shot's sound,
// detached under it. `take` is the new version for the file (takeFromMedia). Returns { ops, shot }.
export function dropOps(project, item, media, index, take) {
  const video = media.kind === "video";
  const len = Number(item.length || 0.1);
  const tIn = round(Number(item.in || 0), 3);
  const out = round(tIn + len, 3);
  const end = Number(media.duration) || 0;
  const pip = Object.fromEntries(PIP_KEYS.filter((k) => item[k] != null).map((k) => [k, item[k]]));
  const shot = {
    id: take.shot,
    name: String(item.name || media.name || "").replace(/\.[a-z0-9]{2,5}$/i, "").slice(0, 120),
    duration: round(clamp(len, 0.2, 600), video ? 3 : 2),
    ...(video ? { trimIn: tIn, trimOut: end && out >= end - 1e-3 ? null : out } : {}),
    ...(Object.keys(pip).length ? { pip } : {}),
  };
  const pick = { [video ? "activeVideo" : "activeImage"]: take.id };
  const list = [{ op: "shot.add", shot, index }, { op: "take.add", take }, { op: "shot.update", id: shot.id, patch: pick }];
  const volume = Number(item.volume ?? 1);
  if (video && media.hasAudio && Math.abs(volume - 1) > 1e-4) {
    const shots = [...project.shots];
    shots.splice(index, 0, { ...shot, ...pick });
    const clip = buildSequence({ ...project, shots, takes: { ...project.takes, [take.id]: take } })
      .find((c) => c.shot.id === shot.id);
    if (clip) {
      list.push({ op: "shot.update", id: shot.id, patch: { audioDetached: true } }, { op: "audio.add", item: {
        id: newId("a"), takeId: take.id, shotId: shot.id, start: round(clip.start, 3), in: round(clip.in, 3),
        out: round(clip.out, 3), name: `${shot.name || "鏡頭"} 原聲`, volume } });
    }
  }
  list.push({ op: "overlay.delete", id: item.id });
  return { ops: list, shot };
}
