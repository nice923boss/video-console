// Library media placed on the timeline: pictures and clips as new shots or on an upper track, sounds on the
// soundtrack. Shared by the library (menus, several at once), the media bin in the edit view and drops on
// the timeline (LB-01, LB-02).
import { ops } from "./actions.js";
import { clamp, newId, round } from "./format.js";
import { audioLength, audioMedia, audioTrack, newOverlay, VOICE_TRACK } from "./sequence.js";

// Library cards carry their media ids under this type while dragged (a JSON list).
export const MEDIA_MIME = "application/x-vc-media";

const TAKE_KEYS = ["kind", "file", "thumb", "strip", "wave", "width", "height", "duration", "fps", "hasAudio"];

export const isVisualMedia = (m) => m.kind === "image" || m.kind === "video";

// Whether a library item matches the search box: its name, note or tags contain the (lower-case) text.
export const mediaMatches = (m, q) =>
  !q || `${m.name || ""} ${m.note || ""} ${(m.tags || []).join(" ")}`.toLowerCase().includes(q);
const stem = (name) => (name || "").replace(/\.[a-z0-9]{2,5}$/i, "").slice(0, 120);

// A take that points at the same files as the media item, so the shot keeps its picture even if the
// item is later removed from the library.
export function takeFromMedia(media, shotId) {
  return {
    id: newId("t"),
    shot: shotId,
    ...Object.fromEntries(TAKE_KEYS.map((k) => [k, media[k] ?? null])),
    created: Math.round(Date.now()) / 1000,
    source: { backend: "import", name: media.name || "" },
    prompt: "",
    negative: "",
    seed: null,
    params: {},
    parent: null,
    favorite: false,
    rating: 0,
    note: "",
    label: stem(media.name),
    jobId: null,
  };
}

// The ops for a new shot at `index` whose picture is the media item.
export function shotOps(media, index) {
  const shot = { id: newId("s"), name: stem(media.name) };
  if (media.kind === "video" && media.duration) shot.duration = round(clamp(Number(media.duration), 0.2, 600), 3);
  const take = takeFromMedia(media, shot.id);
  const key = media.kind === "image" ? "activeImage" : "activeVideo";
  return { shot, list: [{ op: "shot.add", shot, index }, { op: "take.add", take }, { op: "shot.update", id: shot.id, patch: { [key]: take.id } }] };
}

// Whether a sound of `length` at `start` would overlap another clip on music track `key` (voice rows never do).
function busy(audio, project, key, start, length) {
  if (key === VOICE_TRACK) return false;
  return audio.some((a) => {
    const s = Number(a.start || 0);
    return audioTrack(a) === key && start < s + audioLength(a, audioMedia(project, a)) - 1e-3 && s < start + length - 1e-3;
  });
}

// Puts `list` on the timeline in one undo step. Pictures and clips become new shots from `index` on, or go
// on upper track `layer` at `start` ("new": a new track each). Sounds start at `start` on `track` where it is
// free there, otherwise each on a track of its own, as dropped sound files do.
export function placeMedia(project, list, { index, start, layer = null, track = null }, label = "放上時間軸") {
  const out = [];
  const audio = [...(project.audio || [])];
  const { width, height } = project.settings;
  let at = index;
  for (const media of list) {
    if (isVisualMedia(media) && layer) {
      let id = layer;
      if (layer === "new") {
        id = newId("l");
        out.push({ op: "layer.add", layer: { id } });
      }
      out.push({ op: "overlay.add", item: newOverlay(newId("o"), media, id, start, width, height) });
    } else if (isVisualMedia(media)) {
      out.push(...shotOps(media, at).list);
      at += 1;
    } else if (media.kind === "audio") {
      const item = { id: newId("a"), mediaId: media.id, name: (media.name || "").slice(0, 120), start: round(start, 3), volume: media.gain ?? 1 };
      if (track && !busy(audio, project, track, item.start, Math.max(0.1, Number(media.duration) || 0))) item.track = track;
      audio.push(item);
      out.push({ op: "audio.add", item });
    }
  }
  return out.length ? ops(out, { label }) : Promise.resolve(null);
}
