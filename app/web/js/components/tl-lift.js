// Moving a picture between the picture lane and the tracks above it: the drags in tl-tracks.js and the keys
// Ctrl+Alt+Up / Ctrl+Alt+Down (lib/keymap.js "edit.liftUp" / "edit.dropDown"). Lift leaves an empty shot named
// 空白 in the shot's place with its transition (lib/lift.js); a drop by key onto such a gap takes its place and
// its transition back, so nothing after it moves.
import { ops, focusShot } from "../lib/actions.js";
import { setState } from "../lib/store.js";
import { clock } from "../lib/clock.js";
import { newId, round } from "../lib/format.js";
import { liftOps, dropOps } from "../lib/lift.js";
import { layerSpans } from "../lib/track-fit.js";
import { takeFromMedia } from "../lib/media-actions.js";
import { current } from "./tl-commands.js";
import { toast } from "./toast.js";

const EPS = 1e-3;
const MIN_GAP = 0.2; // the shortest shot (opfields duration)
const warn = (text) => toast(text, { level: "warn" });

export function liftShot(project, clip, { target, start }) {
  const res = liftOps(project, clip, target, start);
  if (res.error) return warn(res.error);
  ops(res.ops, { label: "移到上層軌", link: false }).then((ok) => {
    if (ok) setState({ editSel: { type: "overlay", id: res.id } });
  });
}

const selectShot = (id) => {
  setState({ editSel: { type: "shot", id } });
  focusShot(id);
};

// An upper clip dropped on the picture lane becomes a new shot at `index`; the shots from there on move back.
export function overlayToShot(project, item, media, index) {
  const { ops: list, shot } = dropOps(project, item, media, index, takeFromMedia(media, newId("s")));
  ops(list, { label: "移到畫面軌" }).then((res) => {
    if (res) selectShot(shot.id);
  });
}

const free = (spans, s, e) => spans.every((sp) => sp.e <= s + EPS || sp.s >= e - EPS);

// Ctrl+Alt+Up: the selected shot (else the one under the playhead) goes to the lowest shown track above the
// picture lane that is free at the same time, else to a new track; it keeps its time, the same as the drag.
export function liftSelected(project, clips, sel) {
  if (sel && sel.type === "overlay") return warn("這個片段已經在上層軌");
  if (sel && sel.type !== "shot") return warn("先選取畫面軌的鏡頭");
  const cur = current(project, clips, sel);
  if (!cur || !cur.clip) return warn("播放頭下沒有鏡頭");
  const { clip } = cur;
  const layer = (project.layers || []).find((l) => !l.hidden && !l.muted
    && free(layerSpans(project.overlays, l.id), clip.start, clip.start + clip.length));
  liftShot(project, clip, { target: layer ? `layer:${layer.id}` : "new-layer", start: clip.start });
}

// A shot left by a lift: nothing to show and nothing planned for it.
const isGap = (project, c) => c.source === "empty" && !c.shot.prompt && !c.shot.imagePrompt && !c.shot.motion
  && !Object.values(project.takes || {}).some((t) => t.shot === c.shot.id);

function upperAtPlayhead(project) {
  const t = clock.time;
  const under = (project.overlays || []).filter((o) => Number(o.start || 0) <= t + EPS && Number(o.start || 0) + Number(o.length || 0) > t + EPS);
  for (const l of project.layers || []) {
    const o = under.find((x) => x.layer === l.id);
    if (o) return o;
  }
  return null;
}

// The ops that put `item` in place of the gap clip: what the clip leaves free of the gap stays empty before and
// after it (a piece under 0.2 s goes), and the gap's transition stays on the piece that meets the next shot.
function fillOps(project, gap, item, media) {
  const s = Number(item.start || 0);
  const len = Number(item.length || 0);
  const pre = s - gap.start >= MIN_GAP ? s - gap.start : 0;
  const rest = gap.length - len - pre;
  const post = rest >= MIN_GAP ? rest : 0;
  const trans = gap.shot.transition;
  const piece = (duration, transition) => ({ id: newId("s"), name: "空白", duration: round(duration, 3), ...(transition ? { transition } : {}) });
  const pieces = [...(pre ? [piece(pre)] : []), ...(post ? [piece(post, trans)] : [])];
  const list = [{ op: "shot.delete", id: gap.shot.id }, ...pieces.map((shot, k) => ({ op: "shot.add", shot, index: gap.index + k }))];
  const shots = project.shots.filter((x) => x.id !== gap.shot.id);
  shots.splice(gap.index, 0, ...pieces);
  const index = gap.index + (pre ? 1 : 0);
  const { ops: drop, shot } = dropOps({ ...project, shots }, item, media, index, takeFromMedia(media, newId("s")));
  list.push(...drop);
  if (!post && trans) list.push({ op: "shot.update", id: shot.id, patch: { transition: trans } });
  return { list, shot, moved: rest - post > EPS };
}

// Ctrl+Alt+Down: the selected upper clip (else the one under the playhead on the lowest track) goes down to the
// picture lane. Over a 空白 gap that holds it, it takes the gap's place and transition; elsewhere it goes in at
// the nearest cut and the shots after it move back, the same as the drag. insertAt(t): {index} for that cut.
export function dropSelected(project, clips, sel, insertAt) {
  if (sel && sel.type === "shot") return warn("這個鏡頭已經在畫面軌");
  if (sel && sel.type !== "overlay") return warn("先選取上層軌的片段");
  const item = sel ? (project.overlays || []).find((o) => o.id === sel.id) : upperAtPlayhead(project);
  if (!item) return warn("先選取上層軌的片段");
  const media = (project.media || {})[item.mediaId];
  if (!media) return warn("這個片段的檔案不在素材庫，無法移到畫面軌");
  const s = Number(item.start || 0);
  const e = s + Number(item.length || 0);
  const gap = clips.find((c) => isGap(project, c) && c.start <= s + EPS && c.start + c.length >= e - EPS);
  if (!gap) {
    overlayToShot(project, item, media, insertAt(s).index);
    toast("下方沒有空白鏡頭，已穿插到畫面軌，後面的鏡頭往後退", { id: "tl-drop", duration: 2500 });
    return;
  }
  const { list, shot, moved } = fillOps(project, gap, item, media);
  ops(list, { label: "移到畫面軌", link: moved ? null : false }).then((res) => {
    if (res) selectShot(shot.id);
  });
}
