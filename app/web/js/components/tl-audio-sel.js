// Audio clips selected together on the timeline: Ctrl+click adds or removes one, Shift+click takes a range on
// one track. edit-props.js edits the set together.
import { setState, getState } from "../lib/store.js";
import { audioTrack } from "../lib/sequence.js";

// The audio clips selected together (two or more), or null. A Ctrl+click set that no longer holds the
// selected item (another clip or a caption was picked since) no longer counts.
export const multiAudio = (s) => (s.audioSel && s.editSel && s.editSel.type === "audio" && s.audioSel.includes(s.editSel.id)
  ? s.audioSel : null);

// Ctrl+click on an audio clip: add it to the clips selected together, or take it out.
export function toggleAudioSel(id) {
  const s = getState();
  const cur = multiAudio(s) || (s.editSel && s.editSel.type === "audio" ? [s.editSel.id] : []);
  const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
  if (!next.length) return setState({ editSel: null, audioSel: null });
  const keep = next.includes(s.editSel && s.editSel.id) ? s.editSel.id : next[next.length - 1];
  setState({ editSel: { type: "audio", id: keep }, audioSel: next.length > 1 ? next : null });
}

// Shift+click on an audio clip: select every clip on its track from the selected one to it; the selected one
// stays the anchor for the next Shift+click.
export function rangeAudioSel(project, item) {
  const s = getState();
  const anchor = s.editSel && s.editSel.type === "audio" && project.audio.find((a) => a.id === s.editSel.id);
  const track = audioTrack(item);
  if (!anchor || audioTrack(anchor) !== track) return setState({ editSel: { type: "audio", id: item.id }, audioSel: null });
  const [lo, hi] = [Math.min(anchor.start, item.start), Math.max(anchor.start, item.start)];
  const ids = project.audio.filter((a) => audioTrack(a) === track && a.start >= lo && a.start <= hi).map((a) => a.id);
  setState({ audioSel: ids.length > 1 ? ids : null });
}
