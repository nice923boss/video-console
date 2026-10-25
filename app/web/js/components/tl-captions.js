// Caption rows on the edit timeline: the main 字幕 row, then one row per caption track (project.captionTracks, top to
// bottom; the bilingual import adds 中文 above English). Each row hides on its own, and its menu times the row's
// captions to the speech in the clips (vc/align.py), or renames or removes an added row.
import { html } from "../lib/h.js";
import { api } from "../lib/api.js";
import { ops } from "../lib/actions.js";
import { openMenu, confirmDialog, promptDialog } from "./overlay.js";
import { IconButton } from "./ui.js";
import { toast, toastError } from "./toast.js";
import { CaptionTrack, CAPTION_H, rowCaptions } from "./tl-tracks.js";

async function align(project, track, name) {
  try {
    await api.post(`/api/projects/${encodeURIComponent(project.id)}/captions/align`, { track });
    toast(`已開始語音對齊「${name}」，完成後字幕會移到說話的位置（佇列可看進度）`, { level: "success" });
  } catch (err) {
    toastError(err, "無法開始語音對齊");
  }
}

async function rename(t) {
  const name = await promptDialog({ title: "重新命名字幕軌", label: "名稱", value: t.name || "" });
  if (name && name !== t.name) ops([{ op: "captionTrack.update", id: t.id, patch: { name } }], { label: "重新命名字幕軌" });
}

async function remove(t, count) {
  const ok = !count || await confirmDialog({ title: `刪除「${t.name}」字幕軌`, danger: true, confirmLabel: "刪除",
    message: `這條軌道上的 ${count} 則字幕會一起刪除，可以按復原取消。` });
  if (ok) ops([{ op: "captionTrack.delete", id: t.id }], { label: `刪除字幕軌「${t.name}」` });
}

// t: the caption track, or null for the main row.
function menuBtn(project, t) {
  const name = t ? t.name || "字幕" : "字幕";
  const count = rowCaptions(project, t ? t.id : "main").length;
  const items = [
    { label: "語音對齊", icon: "mic", disabled: !count, onClick: () => align(project, t ? t.id : "main", name),
      hint: "依影片裡說的話重新排這條軌道的字幕時間，成對的另一種語言跟著移動" },
    t && { label: "重新命名", icon: "pencil", onClick: () => rename(t) },
    t && { divider: true },
    t && { label: "刪除軌道", icon: "trash", danger: true, onClick: () => remove(t, count) },
  ];
  return html`<${IconButton} icon="more-v" size=${14} title="這條字幕軌的選項" onClick=${(e) => openMenu(e, items)} />`;
}

// row, label, lane, eyeBtn and setTrack are the timeline's own row helpers (components/timeline.js).
export function captionRows({ project, ctx, editSel, tracks, row, label, lane, eyeBtn, setTrack }) {
  const body = (track) => lane("tl-lane-caption",
    html`<${CaptionTrack} project=${project} ctx=${ctx} editSel=${editSel} track=${track} />`);
  const mainHidden = !!tracks.captionsHidden;
  const main = row("captions", CAPTION_H, html`
    ${label("captions", "字幕", html`${eyeBtn(mainHidden,
      () => setTrack("captionsHidden", mainHidden ? "顯示字幕軌" : "隱藏字幕軌"))}${menuBtn(project, null)}`)}
    ${body("main")}`, { hidden: mainHidden });
  return [main, ...(project.captionTracks || []).map((t) => row(`ct:${t.id}`, CAPTION_H, html`
    ${label("captions", t.name || "字幕", html`${eyeBtn(!!t.hidden,
      () => ops([{ op: "captionTrack.update", id: t.id, patch: { hidden: !t.hidden } }],
        { label: `${t.hidden ? "顯示" : "隱藏"}字幕軌「${t.name}」` }))}${menuBtn(project, t)}`)}
    ${body(t.id)}`, { hidden: !!t.hidden }))];
}
