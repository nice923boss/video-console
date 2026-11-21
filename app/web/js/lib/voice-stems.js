// The stems behind the 降噪 / 去回音 / 聲道 of the audio clips and of the shots' own sound (voiceclean.ensure_stems),
// asked for again whenever a clip's settings, trim or file change. The store keeps per clip (an audio clip's id, or
// the shot's for its own sound) voiceStems[id] = {key, made, stems, busy, error}:
// stems are the last ones finished and made the settings they were made for, so the preview keeps playing them
// while a trim within their padding is checked, and plays the source as it is once the settings moved on.
import { api } from "./api.js";
import { getState, setState } from "./store.js";
import { audioMedia } from "./sequence.js";
import { passParams } from "./voice-mix.js";
import { toastError } from "../components/toast.js";

export const stemsParams = (media, item) => JSON.stringify([media ? media.file : "", passParams(item)]);

// A sequence clip plays a sound of its own, which takes the same settings as an audio clip.
export const hasOwnSound = (clip) => clip.source === "video" && !!clip.take.hasAudio && !clip.shot.audioDetached;

// That sound as the audio clip the stems pass, the remix and the 聲道 / 降噪 / 去回音 panel take (mirror of
// voiceclean.shot_sound): the shot's id, its version's file and range, and the shot's settings.
export const shotSound = (clip) => ({
  id: clip.shot.id, takeId: clip.take.id, start: clip.start, in: clip.in, out: clip.out, speed: clip.speed || 1,
  channel: clip.shot.channel || null, denoise: clip.shot.denoise || null, dereverb: clip.shot.dereverb || null,
});

function settle(pid, id, key, patch) {
  setState((s) => {
    const cur = s.pid === pid && (s.voiceStems || {})[id];
    if (!cur || cur.key !== key) return null;
    return { voiceStems: { ...s.voiceStems, [id]: { ...cur, ...patch } } };
  });
}

function fetchStems(pid, id, key, made) {
  api.post(`/api/projects/${encodeURIComponent(pid)}/voice-clean`, { id })
    .then((res) => settle(pid, id, key, { made, stems: res.stems, busy: false, error: "" }))
    .catch((err) => {
      if (err.status === 499) return; // a newer request for this clip took over
      settle(pid, id, key, { busy: false, error: err.message || String(err) });
      toastError(err, "降噪／去回音處理失敗");
    });
}

export function requestStems(project, clips) {
  const was = getState().voiceStems || {};
  const next = {};
  let changed = false;
  const sounds = [...(project.audio || []).map((item) => [item, audioMedia(project, item)]),
    ...clips.filter((c) => hasOwnSound(c) && !c.shot.disabled).map((c) => [shotSound(c), c.take])];
  for (const [item, media] of sounds) {
    if (!media || !passParams(item)) continue;
    const made = stemsParams(media, item);
    const key = JSON.stringify([made, item.in ?? 0, item.out ?? null]);
    const old = was[item.id];
    if (old && old.key === key) {
      next[item.id] = old;
      continue;
    }
    next[item.id] = { key, made: old ? old.made : "", stems: old ? old.stems : null, busy: true, error: "" };
    changed = true;
    fetchStems(project.id, item.id, key, made);
  }
  if (changed || Object.keys(was).length !== Object.keys(next).length) setState({ voiceStems: next });
}
