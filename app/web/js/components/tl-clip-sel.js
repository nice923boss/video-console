// Upper-track clips and adjustment layers selected together on the timeline: Ctrl+click adds or removes one,
// Shift+click takes a range on one track (by start time). Delete, Shift+Delete and V act on all of them
// (tl-commands.js together); edit-props.js lists them. Works like tl-audio-sel.js does for audio clips.
import { setState, getState } from "../lib/store.js";

const LISTS = { overlay: "overlays", adjust: "adjustments" };
export const CLIP_SEL_TYPES = Object.keys(LISTS);

// The ids selected together (two or more) when the selected item is an upper-track clip or adjustment layer
// among them, else null: picking anything else since then drops the set.
export const multiClip = (s) => (s.clipSel && s.editSel && LISTS[s.editSel.type] && s.clipSel.includes(s.editSel.id)
  ? s.clipSel : null);

// Ctrl+click: add the clip to the ones selected together, or take it out. A clip of the other kind starts over.
export function toggleClipSel(type, id) {
  const s = getState();
  const same = s.editSel && s.editSel.type === type;
  const cur = same ? multiClip(s) || [s.editSel.id] : [];
  const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
  if (!next.length) return setState({ editSel: null, clipSel: null });
  const keep = same && next.includes(s.editSel.id) ? s.editSel.id : next[next.length - 1];
  setState({ editSel: { type, id: keep }, clipSel: next.length > 1 ? next : null });
}

// The track an item sits on: an upper-track clip's picture track; adjustment layers share one lane.
const trackOf = (type, item) => (type === "overlay" ? item.layer || "" : "adjust");

// Shift+click: every clip on the track from the selected one to this one, by start time. The selected one
// stays the anchor for the next Shift+click; with nothing of this kind selected on the track, just this one.
export function rangeClipSel(project, type, item) {
  const s = getState();
  const list = project[LISTS[type]] || [];
  const anchor = s.editSel && s.editSel.type === type && list.find((x) => x.id === s.editSel.id);
  const track = trackOf(type, item);
  if (!anchor || trackOf(type, anchor) !== track) return setState({ editSel: { type, id: item.id }, clipSel: null });
  const at = (x) => Number(x.start || 0);
  const [lo, hi] = [Math.min(at(anchor), at(item)), Math.max(at(anchor), at(item))];
  const ids = list.filter((x) => trackOf(type, x) === track && at(x) >= lo && at(x) <= hi).map((x) => x.id);
  setState({ clipSel: ids.length > 1 ? ids : null });
}

// A click without dragging on an upper-track clip or adjustment layer, with the modifiers held.
export function clickClip(ev, project, type, item) {
  if (ev && (ev.ctrlKey || ev.metaKey)) toggleClipSel(type, item.id);
  else if (ev && ev.shiftKey) rangeClipSel(project, type, item);
  else setState({ editSel: { type, id: item.id }, clipSel: null });
}
