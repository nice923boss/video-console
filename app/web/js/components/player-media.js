// Media element helpers of the preview player (player-engine.js), and the preview side of an audio clip's
// 降噪 / 去回音 / 聲道: beside the element playing its source file, such a clip gets a second one for its stems
// (lib/voice-stems.js), which the mix remixes (lib/audio-mix.js buildVoice).
import { fileUrl } from "../lib/format.js";
import { mixPlan, stemChannels } from "../lib/voice-mix.js";
import { stemsParams } from "../lib/voice-stems.js";

export function release(media) {
  media.pause();
  if (media.hasAttribute("src")) {
    media.removeAttribute("src");
    media.load();
  }
}

export function playSafe(media) {
  const p = media.play();
  if (!p) return;
  p.catch((err) => {
    // AbortError: a pause() arrived before playback started, which is normal while scrubbing.
    // NotAllowedError: the browser refused sound without a user gesture; keep the picture moving.
    if (err && err.name === "NotAllowedError" && !media.muted) {
      media.muted = true;
      media.play().catch(() => {});
    }
  });
}

export function seekTo(media, holder, target) {
  if (media.seeking) {
    holder.pendingSeek = target;
    return;
  }
  holder.pendingSeek = null;
  try {
    media.currentTime = target;
  } catch {
    // Not seekable yet; loadedmetadata re-renders and seeks again.
  }
}

// Every element of an audio clip's track entry.
export const trackElements = (tr) => (tr.fx ? [tr.audio, tr.fx.audio] : [tr.audio]);

// What the clip plays at `target` (source seconds): {el, holder (its pendingSeek), at (el's time), voice (for
// routeGain)}. Its stems when `entry` (voiceStems[id], falsy without a running mix) was made for its settings as
// they are now and holds target; otherwise its source file as it is. The element not chosen is paused.
// mode: "fx" | "dry" | "delta", see audio-mix.js tuneVoice.
export function voicePlay(tr, target, pid, entry, mode) {
  const s = entry && entry.stems;
  const use = s && entry.made === stemsParams(tr.media, tr.item) && target >= s.from && target < s.to;
  if (!use) {
    if (tr.fx && !tr.fx.audio.paused) tr.fx.audio.pause();
    return { el: tr.audio, holder: tr, at: target, voice: null };
  }
  if (!tr.fx) {
    const audio = new Audio();
    audio.preload = "auto";
    tr.fx = { audio, src: "", pendingSeek: null };
  }
  const src = fileUrl(pid, s.file);
  if (tr.fx.src !== src) {
    tr.fx.src = src;
    tr.fx.audio.src = src;
  }
  if (!tr.audio.paused) tr.audio.pause();
  const voice = { channels: stemChannels(tr.item), plan: mixPlan(tr.item), monitor: mode };
  return { el: tr.fx.audio, holder: tr.fx, at: target - s.from, voice };
}
