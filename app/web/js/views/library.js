// Library view: media imported into the project (pictures, clips, music) and, on the second tab, the
// files exported from it. Media can be previewed, renamed, starred, put on the soundtrack, given to a
// shot as its picture or reference, revealed on disk or removed (undoable).
import { html, useState, useEffect, useMemo, useRef } from "../lib/h.js";
import { useStore, setState, getState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { ops, undo, navigate, focusShot, revealInFolder } from "../lib/actions.js";
import { uploadFiles, importFiles, relinkFile } from "../lib/jobs-actions.js";
import { clock } from "../lib/clock.js";
import { useKeymap, keyLabel, undoKeyText, confirmKeyText } from "../lib/keymap.js";
import { usePref } from "../lib/prefs.js";
import { fileUrl, fmtBytes, fmtDuration, fmtTime, newId, round, shotLabel, ACCEPT_ALL, ACCEPT_AUDIO, ACCEPT_IMAGE, ACCEPT_VISUAL }
  from "../lib/format.js";
import { shotCover, newOverlay } from "../lib/sequence.js";
import { fitStart, layerSpans } from "../lib/track-fit.js";
import { takeFromMedia, shotOps, placeMedia, mediaMatches } from "../lib/media-actions.js";
import { restoreVoiceOps } from "../lib/voice.js";
import { Button, IconButton, Chip, Select, Tabs, Empty, Spinner, Progress, Badge } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { Thumb, mediaEntry, openLightbox } from "../components/media.js";
import { openMenu, openModal, Modal, confirmDialog, promptDialog } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { ExportsPane } from "./library-exports.js";

const enc = encodeURIComponent;
const KIND_ICON = { image: "image", video: "film", audio: "music" };
const KIND_NAME = { image: "圖片", video: "影片", audio: "音訊" };
const FILTERS = [
  { id: "all", label: "全部", test: () => true },
  { id: "image", label: "圖片", icon: "image", test: (m) => m.kind === "image" },
  { id: "video", label: "影片", icon: "film", test: (m) => m.kind === "video" },
  { id: "audio", label: "音訊", icon: "music", test: (m) => m.kind === "audio" },
  { id: "favorite", label: "最愛", icon: "star", test: (m) => !!m.favorite },
  { id: "unused", label: "沒用到", icon: "inbox", test: (m, unused) => unused.has(m.id) },
  { id: "trash", label: "垃圾桶", icon: "trash", test: () => true },
];
// The project trash (AU-14) holds replaced voice-overs; they show under its chip only.
const shows = (f, m, unused) => (f.id === "trash") === !!m.trashed && f.test(m, unused);
const SORTS = [
  { value: "new", label: "最新匯入" },
  { value: "old", label: "最早匯入" },
  { value: "name", label: "名稱" },
  { value: "duration", label: "長度" },
];
const SORT_FN = {
  new: (a, b) => (b.created || 0) - (a.created || 0),
  old: (a, b) => (a.created || 0) - (b.created || 0),
  name: (a, b) => (a.name || "").localeCompare(b.name || "", "zh-Hant"),
  duration: (a, b) => (b.duration || 0) - (a.duration || 0),
};

const countBy = (list, key) => list.reduce((m, x) => (x[key] ? m.set(x[key], (m.get(x[key]) || 0) + 1) : m), new Map());

// Picks a shot (close(shotId)), or in insert mode a position between shots (close(index into project.shots)).
function ShotPicker({ project, title, insert, close }) {
  const ratio = `${project.settings.width}/${project.settings.height}`;
  const shots = project.shots;
  const body = (s, i) => {
    const cover = shotCover(project, s);
    return html`<${Thumb} pid=${project.id} path=${cover ? cover.path : ""} ratio=${ratio} icon="frame" />
      <span class="ref-cell-label">${shotLabel(s, i)}</span>`;
  };
  const gapTitle = (i) => (i === 0 ? `插在 ${shotLabel(shots[0], 0)} 前面`
    : i === shots.length ? `插在 ${shotLabel(shots[i - 1], i - 1)} 後面`
      : `插在 ${shotLabel(shots[i - 1], i - 1)} 與 ${shotLabel(shots[i], i)} 之間`);
  const gap = (i) => html`<button type="button" key=${`g${i}`} class="ins-gap" title=${gapTitle(i)} aria-label=${gapTitle(i)}
    onClick=${() => close(i)}><${Icon} name="plus" size=${14} /></button>`;
  return html`<${Modal} title=${title} wide onClose=${() => close(null)}>
    ${!shots.length
      ? html`<${Empty} icon="frame" title="專案裡還沒有鏡頭">先到分鏡頁新增鏡頭<//>`
      : insert
        ? html`<p class="ins-hint">點鏡頭之間的 ＋ 決定放的位置</p>
          <div class="ins-row">
            ${shots.map((s, i) => [gap(i), html`<div key=${s.id} class="ref-cell" title=${shotLabel(s, i)}>${body(s, i)}</div>`])}
            ${gap(shots.length)}
          </div>`
        : html`<div class="ref-grid">
          ${shots.map((s, i) => html`<button type="button" key=${s.id} class="ref-cell" title=${shotLabel(s, i)}
            onClick=${() => close(s.id)}>${body(s, i)}</button>`)}
        </div>`}
  <//>`;
}

const pickShot = (project, title) =>
  openModal((close) => html`<${ShotPicker} project=${project} title=${title} close=${close} />`);

const pickGap = (project, title) => (project.shots.length
  ? openModal((close) => html`<${ShotPicker} project=${project} title=${title} insert close=${close} />`)
  : Promise.resolve(0));

async function assignToShot(project, media, shotId) {
  const index = project.shots.findIndex((s) => s.id === shotId);
  if (index < 0) return;
  const take = takeFromMedia(media, shotId);
  const key = media.kind === "image" ? "activeImage" : "activeVideo";
  const res = await ops([{ op: "take.add", take }, { op: "shot.update", id: shotId, patch: { [key]: take.id } }],
    { label: "指定素材給鏡頭" });
  if (res) {
    toast(`已把「${media.name}」設為 ${shotLabel(project.shots[index], index)} 的${media.kind === "image" ? "圖片" : "影片"}`, {
      level: "success", action: { label: "查看", fn: () => focusShot(shotId, { tab: "takes", view: "board" }) },
    });
  }
}

// A new shot at `index` whose picture is the media item, so it slots in between existing shots.
async function insertAsShot(project, media, index) {
  const { shot, list } = shotOps(media, index);
  const res = await ops(list, { label: "穿插素材為新鏡頭" });
  if (res) {
    toast(`已把「${media.name}」穿插為第 ${index + 1} 個鏡頭`, {
      level: "success", action: { label: "查看", fn: () => focusShot(shot.id, { tab: "takes", view: "board" }) },
    });
  }
}

const layerName = (layer, index) => (layer && layer.name) || `畫面 ${index + 2}`;

// Puts a picture or clip on a track above the picture lane (a new one on top when layerId is null), in the
// nearest free stretch of an existing track.
async function addToLayer(project, media, layerId, start) {
  const layers = project.layers || [];
  const list = [];
  let layer = layerId;
  if (!layer) {
    layer = newId("l");
    list.push({ op: "layer.add", layer: { id: layer } });
  }
  const placed = newOverlay(newId("o"), media, layer, start, project.settings.width, project.settings.height);
  const item = layerId ? { ...placed, start: round(fitStart(placed.start, placed.length, layerSpans(project.overlays, layerId)), 3) } : placed;
  list.push({ op: "overlay.add", item });
  const res = await ops(list, { label: "放到畫面軌" });
  if (res) {
    const index = layerId ? layers.findIndex((l) => l.id === layerId) : layers.length;
    toast(`已放到${layerName(layers[index], index)}，從 ${fmtTime(item.start)} 開始`, {
      level: "success", action: { label: "到剪輯頁", fn: () => setState({ view: "edit", editSel: { type: "overlay", id: item.id } }) },
    });
  }
}

async function setAsRef(project, media, shotId) {
  const index = project.shots.findIndex((s) => s.id === shotId);
  if (index < 0) return;
  const res = await ops([{ op: "shot.update", id: shotId, patch: { ref: media.id } }], { label: "設定參考圖" });
  if (res) toast(`已設為 ${shotLabel(project.shots[index], index)} 的參考圖`, { level: "success" });
}

async function addToTrack(media, start) {
  const item = { id: newId("a"), mediaId: media.id, name: (media.name || "").slice(0, 120), start: round(start, 3), volume: media.gain ?? 1 };
  const res = await ops([{ op: "audio.add", item }], { label: "加入音軌" });
  if (res) {
    toast(`已加入音軌，從 ${fmtTime(start)} 開始`, {
      level: "success", action: { label: "到剪輯頁", fn: () => setState({ view: "edit", editSel: { type: "audio", id: item.id } }) },
    });
  }
}

async function rename(media) {
  const name = await promptDialog({ title: "重新命名素材", label: "名稱", value: media.name || "", confirmLabel: "儲存",
    validate: (v) => (!v ? "請輸入名稱" : v.length > 200 ? "名稱最多 200 字" : "") });
  if (name && name !== media.name) ops([{ op: "media.update", id: media.id, patch: { name } }], { label: "重新命名素材" });
}

async function editNote(media) {
  const note = await promptDialog({ title: "素材備註", label: "備註", value: media.note || "", multiline: true,
    confirmLabel: "儲存", hint: [confirmKeyText("儲存"), "留白代表清除備註"].filter(Boolean).join("，"), validate: (v) => (v.length > 1000 ? "備註最多 1000 字" : "") });
  if (note !== null && note !== (media.note || "")) ops([{ op: "media.update", id: media.id, patch: { note } }], { label: "編輯素材備註" });
}

// Tags (LB-04), typed as words split by commas, 、 or spaces.
const MAX_TAGS = 20;
const splitTags = (text) => [...new Set(text.split(/[,，、\s]+/).map((t) => t.slice(0, 30)).filter(Boolean))];

async function editTags(media) {
  const text = await promptDialog({ title: "素材標籤", label: "標籤", value: (media.tags || []).join("、"), confirmLabel: "儲存",
    hint: "用逗號、頓號或空白分開，例如：海邊、人物、夜景；留白代表清除",
    validate: (v) => (splitTags(v).length > MAX_TAGS ? `每個素材最多 ${MAX_TAGS} 個標籤` : "") });
  if (text === null) return;
  const tags = splitTags(text);
  if (tags.join("\n") !== (media.tags || []).join("\n")) {
    ops([{ op: "media.update", id: media.id, patch: { tags } }], { label: "編輯素材標籤" });
  }
}

async function tagMany(list) {
  const text = await promptDialog({ title: `替 ${list.length} 個素材加標籤`, label: "標籤", confirmLabel: "加上",
    hint: "用逗號、頓號或空白分開；已有的標籤會保留", validate: (v) => (splitTags(v).length ? "" : "請輸入標籤") });
  if (!text) return;
  const add = splitTags(text);
  const changed = list.map((m) => ({ m, tags: [...new Set([...(m.tags || []), ...add])].slice(0, MAX_TAGS) }))
    .filter(({ m, tags }) => tags.length !== (m.tags || []).length);
  if (changed.length) ops(changed.map(({ m, tags }) => ({ op: "media.update", id: m.id, patch: { tags } })), { label: "加上素材標籤" });
}

const toggleFavorite = (media) =>
  ops([{ op: "media.update", id: media.id, patch: { favorite: !media.favorite } }], { label: media.favorite ? "取消最愛" : "加入最愛" });

// Items nothing else in the project mentions: not on a track, a layer, a shot (as picture or reference),
// a cast member or anywhere else. Imported takes keep the media id in their file path, so they count too.
// So does the footage of a stabilized copy: unticking 防抖 on an upper-track clip goes back to it.
function unusedMedia(project) {
  const stabbed = Object.values(project.media || {}).filter((m) => m.source && m.source.tool === "stabilize");
  const rest = JSON.stringify({ ...project, media: stabbed.map((m) => m.parent) });
  return new Set(Object.keys(project.media || {}).filter((id) => !rest.includes(id)));
}

async function removeUnused(list) {
  const ok = await confirmDialog({ title: "移除沒用到的素材", danger: true, confirmLabel: `移除 ${list.length} 個`,
    message: `會從素材庫移除 ${list.length} 個沒放進任何鏡頭、音軌或畫面軌的素材，之後仍可${undoKeyText()}復原。`
      + "檔案會保留到按下方「清除」為止。" });
  if (!ok) return;
  const res = await ops(list.map((m) => ({ op: "media.delete", id: m.id })), { label: "移除沒用到的素材" });
  if (res) toast(`已移除 ${list.length} 個素材`, { action: { label: "復原", fn: undo } });
}

async function removeMedia(media, uses) {
  const parts = [uses.track && `音軌上的 ${uses.track} 段`, uses.pip && `上層畫面軌上的 ${uses.pip} 段`,
    uses.refs && `${uses.refs} 個鏡頭的參考圖`].filter(Boolean);
  if (parts.length) {
    const ok = await confirmDialog({ title: "刪除素材", danger: true, confirmLabel: "刪除",
      message: `「${media.name}」正在使用中，刪除後${parts.join("與")}也會一併移除。之後仍可${undoKeyText()}復原。` });
    if (!ok) return;
  }
  const res = await ops([{ op: "media.delete", id: media.id }], { label: "刪除素材" });
  if (res) toast(`已刪除「${media.name}」`, { action: { label: "復原", fn: undo } });
}

// Several items at once (LB-01), each a single undo step.
function favoriteMany(list) {
  const on = !list.every((m) => m.favorite);
  return ops(list.filter((m) => !!m.favorite !== on).map((m) => ({ op: "media.update", id: m.id, patch: { favorite: on } })),
    { label: on ? "加入最愛" : "取消最愛" });
}

async function placeMany(project, list) {
  const res = await placeMedia(project, list, { index: project.shots.length, start: clock.time }, "加到時間軸");
  if (res) {
    toast(`已把 ${list.length} 個素材加到時間軸：圖片與影片接在最後，聲音從 ${fmtTime(clock.time)} 開始`, {
      level: "success", action: { label: "到剪輯頁", fn: () => setState({ view: "edit" }) },
    });
  }
}

async function removeMany(list, uses) {
  const busy = list.filter((m) => { const u = uses(m); return u.track || u.pip || u.refs; }).length;
  const ok = await confirmDialog({ title: "刪除素材", danger: true, confirmLabel: `刪除 ${list.length} 個`,
    message: `會從素材庫刪除 ${list.length} 個素材${busy ? `，其中 ${busy} 個正在音軌、畫面軌或參考圖上使用，那些地方也會一併移除` : ""}。`
      + `之後仍可${undoKeyText()}復原。` });
  if (!ok) return;
  const res = await ops(list.map((m) => ({ op: "media.delete", id: m.id })), { label: "刪除素材" });
  if (res) toast(`已刪除 ${list.length} 個素材`, { action: { label: "復原", fn: undo } });
}

// Point an item at another file (LB-09): moved or renamed on disk, or a better version of the same thing.
function relink(media) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = { image: ACCEPT_IMAGE, audio: ACCEPT_AUDIO }[media.kind]
    || ACCEPT_VISUAL.split(",").filter((x) => !ACCEPT_IMAGE.split(",").includes(x)).join(",");
  input.onchange = () => input.files[0] && relinkFile(media, input.files[0]);
  input.click();
}

function download(pid, media) {
  const a = document.createElement("a");
  a.href = fileUrl(pid, media.file, { download: true, name: media.name || "" });
  a.rel = "noopener";
  a.click();
}

// A replaced voice-over in the project trash (AU-14): back on its line, back in the library, or deleted.
function trashMenu(e, ctx, media) {
  const { project, uses, preview } = ctx;
  const cap = (project.captions || []).find((c) => c.id === (media.source || {}).captionId);
  const back = (list, label) => ops(list, { label }).then((res) => res && toast(`已${label}`, { action: { label: "復原", fn: undo } }));
  openMenu(e, [
    { header: media.name || KIND_NAME[media.kind] },
    { label: "預覽", icon: "eye", hint: keyLabel("library.preview"), onClick: () => preview(media) },
    { divider: true },
    cap && { label: "換回這句字幕的配音", icon: "swap", onClick: () => back(restoreVoiceOps(project, media, cap), "換回舊配音") },
    { label: "移回素材庫", icon: "inbox", onClick: () => back([{ op: "media.update", id: media.id, patch: { trashed: false } }], "移回素材庫") },
    { divider: true },
    { label: "刪除", icon: "trash", danger: true, hint: keyLabel("library.delete"), onClick: () => removeMedia(media, uses(media)) },
  ]);
}

function mediaMenu(e, ctx, media) {
  if (media.trashed) return trashMenu(e, ctx, media);
  const { project, uses, preview } = ctx;
  const focusIndex = project.shots.findIndex((s) => s.id === getState().focus);
  const focus = focusIndex >= 0 ? project.shots[focusIndex] : null;
  const visual = media.kind === "image" || media.kind === "video";
  const what = media.kind === "image" ? "圖片" : "影片";
  openMenu(e, [
    { header: media.name || KIND_NAME[media.kind] },
    { label: "預覽", icon: "eye", hint: keyLabel("library.preview"), onClick: () => preview(media) },
    { label: "重新命名", icon: "pencil", hint: keyLabel("library.rename"), onClick: () => rename(media) },
    { label: media.note ? "編輯備註" : "加上備註", icon: "clipboard", onClick: () => editNote(media) },
    { label: media.tags?.length ? "編輯標籤" : "加上標籤", icon: "hash", onClick: () => editTags(media) },
    { label: media.favorite ? "取消最愛" : "加入最愛", icon: "star", hint: keyLabel("library.favorite"), checked: !!media.favorite,
      onClick: () => toggleFavorite(media) },
    { divider: true },
    media.kind === "audio" && { label: `加到音軌（播放頭 ${fmtTime(clock.time)}）`, icon: "music",
      onClick: () => addToTrack(media, clock.time) },
    media.kind === "audio" && { label: "加到音軌開頭", icon: "skip-back", onClick: () => addToTrack(media, 0) },
    visual && focus && { label: `設為 ${shotLabel(focus, focusIndex)} 的${what}`, icon: "frame",
      onClick: () => assignToShot(project, media, focus.id) },
    visual && { label: `設為鏡頭的${what}…`, icon: "grid",
      onClick: () => pickShot(project, `把「${media.name}」設為哪個鏡頭的${what}？`).then((id) => id && assignToShot(project, media, id)) },
    visual && { label: "穿插為新鏡頭…", icon: "plus",
      onClick: () => pickGap(project, `把「${media.name}」穿插在哪裡？`).then((i) => i != null && insertAsShot(project, media, i)) },
    visual && { label: `放到新的畫面軌（播放頭 ${fmtTime(clock.time)}）`, icon: "layers",
      onClick: () => addToLayer(project, media, null, clock.time) },
    ...(visual ? (project.layers || []).map((l, i) => ({ label: `放到${layerName(l, i)}（播放頭 ${fmtTime(clock.time)}）`,
      icon: "layers", onClick: () => addToLayer(project, media, l.id, clock.time) })).reverse() : []),
    media.kind === "image" && focus && { label: `當作 ${shotLabel(focus, focusIndex)} 的參考圖`, icon: "link",
      onClick: () => setAsRef(project, media, focus.id) },
    media.kind === "image" && { label: "當作鏡頭的參考圖…", icon: "link",
      onClick: () => pickShot(project, "當作哪個鏡頭的參考圖？").then((id) => id && setAsRef(project, media, id)) },
    { divider: true },
    { label: "在檔案總管中顯示", icon: "folder-open", onClick: () => revealInFolder(media.file) },
    { label: "下載原始檔", icon: "download", onClick: () => download(project.id, media) },
    { label: "重新連結檔案…", icon: "link", onClick: () => relink(media) },
    { divider: true },
    { label: "刪除", icon: "trash", danger: true, hint: keyLabel("library.delete"), onClick: () => removeMedia(media, uses(media)) },
  ]);
}

function MediaCard({ media, project, selected, missing, uses, onSelect, onOpen, onMenu }) {
  const u = uses(media);
  const isAudio = media.kind === "audio";
  const tags = [
    u.track > 0 && html`<span key="t" title="在音軌上"><${Icon} name="music" size=${11} />${u.track}</span>`,
    u.pip > 0 && html`<span key="p" title="在上層畫面軌上"><${Icon} name="layers" size=${11} />${u.pip}</span>`,
    u.refs > 0 && html`<span key="r" title="當作參考圖"><${Icon} name="link" size=${11} />${u.refs}</span>`,
    u.shots > 0 && html`<span key="s" title="已設為鏡頭畫面"><${Icon} name="frame" size=${11} />${u.shots}</span>`,
  ].filter(Boolean);
  const meta = [
    media.width && media.height ? `${media.width}×${media.height}` : "",
    media.kind !== "image" && media.duration ? fmtDuration(media.duration) : "",
  ].filter(Boolean).join(" · ");
  return html`<div class=${`lib-card kind-${media.kind} ${selected ? "selected" : ""}`} data-media=${media.id} tabindex="0"
      role="option" aria-selected=${selected ? "true" : "false"} title=${media.note || media.name}
      onClick=${(e) => onSelect(media.id, e)} onDblClick=${() => onOpen(media)}
      onContextMenu=${(e) => { onSelect(media.id); onMenu(e, media); }}>
    ${isAudio
      ? html`<div class="lib-audio-ph"><${Icon} name="music" size=${28} /><span>${fmtDuration(media.duration)}</span></div>`
      : html`<${Thumb} pid=${project.id} path=${media.thumb || (media.kind === "image" ? media.file : "")} strip=${media.strip || ""}
          video=${media.kind === "video" ? media.file : ""} ratio="16/10" icon=${KIND_ICON[media.kind] || "file"} class="lib-thumb" />`}
    <span class="lib-kind"><${Icon} name=${KIND_ICON[media.kind] || "file"} size=${12} /></span>
    <button type="button" class=${`lib-fav ${media.favorite ? "on" : ""}`} title=${media.favorite ? "取消最愛" : "加入最愛"}
      aria-label=${media.favorite ? "取消最愛" : "加入最愛"}
      onClick=${(e) => { e.stopPropagation(); toggleFavorite(media); }} onDblClick=${(e) => e.stopPropagation()}>
      <${Icon} name="star" size=${14} /></button>
    <div class="lib-body">
      <div class="lib-row">
        <span class="lib-name">${media.name || "未命名"}</span>
        <button type="button" class="icon-btn lib-more" title="更多動作" aria-label="更多動作"
          onClick=${(e) => { onSelect(media.id); onMenu(e, media); }} onDblClick=${(e) => e.stopPropagation()}>
          <${Icon} name="more" size=${15} /></button>
      </div>
      <div class="lib-meta">
        <span class="dim">${meta || KIND_NAME[media.kind]}</span>
        ${tags.length > 0 && html`<span class="lib-tags">${tags}</span>`}
        ${missing && html`<button type="button" class="lib-missing" title="原始檔已被移動或刪除，點一下選新的檔案"
          onClick=${(e) => { e.stopPropagation(); relink(media); }} onDblClick=${(e) => e.stopPropagation()}>
          <${Icon} name="alert" size=${11} />找不到檔案</button>`}
      </div>
      ${media.tags?.length > 0 && html`<div class="lib-labels">${media.tags.map((t) => html`<span key=${t}>${t}</span>`)}</div>`}
    </div>
  </div>`;
}

function PendingCard({ job }) {
  const running = job.status === "running";
  return html`<div class="lib-card is-pending" title=${job.title}>
    <div class="lib-audio-ph"><${Spinner} size=${22} /><span>${running ? `處理中 ${Math.round((job.progress || 0) * 100)}%` : "排隊中"}</span></div>
    <div class="lib-body"><div class="lib-row"><span class="lib-name">${job.title}</span></div>
      <${Progress} value=${running ? job.progress || 0 : null} /></div>
  </div>`;
}

function UsageBar({ pid, rev }) {
  const [info, setInfo] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    const t = setTimeout(() => {
      api.get(`/api/projects/${enc(pid)}/usage`).then((d) => alive && setInfo(d), () => alive && setInfo(null));
    }, 600);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [pid, rev]);
  if (!info) return null;
  const purge = async () => {
    const ok = await confirmDialog({ title: "清除未使用的檔案", danger: true, confirmLabel: "永久清除",
      message: `會永久刪除 ${info.orphans} 個沒有任何版本或素材使用的檔案與一天以上的暫存檔（約 ${fmtBytes(info.unused)}），`
        + (info.trashed ? `清空素材垃圾桶裡的 ${info.trashed} 個舊配音，` : "")
        + "並清空這個專案的復原紀錄，已刪除的版本之後就無法再救回。最近 5 分鐘內產生的檔案不受影響。" });
    if (!ok) return;
    setBusy(true);
    try {
      const res = await api.post(`/api/projects/${enc(pid)}/purge`);
      toast(`已清除 ${fmtBytes(res.freed)}`, { level: "success" });
      setInfo({ ...info, unused: 0, orphans: 0, trashed: 0 });
    } catch (err) {
      toastError(err, "清除失敗");
    } finally {
      setBusy(false);
    }
  };
  return html`<div class="lib-usage">
    <${Icon} name="drive" size=${15} />
    <span>專案媒體 <b>${fmtBytes(info.used + info.unused)}</b></span>
    ${info.orphans > 0 || info.trashed > 0
      ? html`<span class="dim">其中 ${fmtBytes(info.unused)} 是已刪除的版本、素材、垃圾桶裡的舊配音與暫存檔（${info.orphans} 個）</span>
          <${Button} size="sm" kind="ghost" icon="trash" busy=${busy} onClick=${purge}>清除<//>`
      : html`<span class="dim">沒有多餘的檔案</span>`}
  </div>`;
}

function MediaPane({ project }) {
  const jobs = useStore((s) => s.jobs);
  const [filter, setFilter] = usePref("vc.lib.filter", "all", (v) => FILTERS.some((f) => f.id === v));
  const [sort, setSort] = usePref("vc.lib.sort", "new", (v) => v in SORT_FN);
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState("");
  // sel: the current item (keys act on it, Shift-click ranges start from it); picked: everything selected.
  const [sel, setCurrent] = useState(null);
  const [picked, setPicked] = useState(() => new Set());
  const [dropping, setDropping] = useState(false);
  const fileRef = useRef(null);
  const gridRef = useRef(null);
  const pid = project.id;

  const all = useMemo(() => Object.values(project.media || {}), [project.media]);
  const uses = useMemo(() => {
    const track = countBy(project.audio || [], "mediaId");
    const pip = countBy(project.overlays || [], "mediaId");
    const refs = countBy(project.shots, "ref");
    const shots = countBy(Object.values(project.takes).filter((t) => t.source && t.source.backend === "import"), "file");
    return (m) => ({ track: track.get(m.id) || 0, pip: pip.get(m.id) || 0, refs: refs.get(m.id) || 0, shots: shots.get(m.file) || 0 });
  }, [project.audio, project.overlays, project.shots, project.takes]);
  const unused = useMemo(() => unusedMedia(project), [project]);
  // Items whose file is gone from disk (LB-09), asked again whenever a file changes.
  const [missing, setMissing] = useState(() => new Set());
  const files = all.map((m) => m.file).join("|");
  useEffect(() => {
    let alive = true;
    api.get(`/api/projects/${enc(pid)}/missing`).then((d) => alive && setMissing(new Set(d.media)), () => {});
    return () => {
      alive = false;
    };
  }, [pid, files]);
  const counts = useMemo(() => Object.fromEntries(FILTERS.map((f) => [f.id, all.filter((m) => shows(f, m, unused)).length])),
    [all, unused]);
  const allTags = useMemo(() => [...new Set(all.flatMap((m) => m.tags || []))].sort((a, b) => a.localeCompare(b, "zh-Hant")),
    [all]);
  const list = useMemo(() => {
    const f = FILTERS.find((x) => x.id === filter) || FILTERS[0];
    const q = query.trim().toLowerCase();
    return all.filter((m) => shows(f, m, unused) && mediaMatches(m, q) && (!tag || (m.tags || []).includes(tag)))
      .sort(SORT_FN[sort]);
  }, [all, unused, filter, query, sort, tag]);
  useEffect(() => {
    if (tag && !allTags.includes(tag)) setTag("");
  }, [allTags]);
  const pending = jobs.filter((j) => j.pid === pid && j.kind === "ingest" && !j.shotId
    && (j.status === "queued" || j.status === "running"));

  const setSel = (id) => {
    setCurrent(id);
    setPicked(new Set(id ? [id] : []));
  };
  // Plain click picks one, Ctrl toggles one, Shift takes the run from the current item. A right-click on an
  // item that is already picked keeps the others (no event is passed then).
  const select = (id, e = null) => {
    if (!e) return picked.has(id) ? setCurrent(id) : setSel(id);
    if (e.shiftKey && sel) {
      const a = list.findIndex((m) => m.id === sel);
      const b = list.findIndex((m) => m.id === id);
      if (a >= 0 && b >= 0) return setPicked(new Set(list.slice(Math.min(a, b), Math.max(a, b) + 1).map((m) => m.id)));
    }
    if (e.ctrlKey || e.metaKey) {
      const next = new Set(picked);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      setCurrent(id);
      return setPicked(next);
    }
    return setSel(id);
  };
  useEffect(() => {
    if (sel && !project.media[sel]) setCurrent(null);
    if ([...picked].some((id) => !project.media[id])) setPicked(new Set([...picked].filter((id) => project.media[id])));
  }, [project.media]);
  useEffect(() => {
    const el = sel && gridRef.current && gridRef.current.querySelector(`[data-media="${sel}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const preview = (media) => {
    const visible = list.filter((m) => m.file);
    const entries = visible.map((m) => mediaEntry(m));
    openLightbox(pid, entries, Math.max(0, visible.indexOf(media)));
  };
  const ctx = { project, uses, preview };
  const current = sel ? project.media[sel] : null;
  const many = list.filter((m) => picked.has(m.id));
  const bulk = many.length > 1 ? many : null;
  const step = (d) => {
    if (!list.length) return;
    const i = list.findIndex((m) => m.id === sel);
    setSel(list[Math.min(list.length - 1, Math.max(0, i < 0 ? 0 : i + d))].id);
  };
  useKeymap({
    "library.preview": () => (current ? preview(current) : false),
    "library.favorite": () => (bulk ? favoriteMany(bulk) : current ? toggleFavorite(current) : false),
    "library.rename": () => (current ? rename(current) : false),
    "library.delete": () => (bulk ? removeMany(bulk, uses) : current ? removeMedia(current, uses(current)) : false),
    "library.prev": () => step(-1),
    "library.next": () => step(1),
    "library.first": () => list.length && setSel(list[0].id),
    "library.last": () => list.length && setSel(list[list.length - 1].id),
    "library.deselect": () => (sel || picked.size ? setSel(null) : false),
    "library.selectAll": () => {
      if (!list.length) return false;
      setCurrent(sel && picked.has(sel) ? sel : list[0].id);
      return setPicked(new Set(list.map((m) => m.id)));
    },
  });

  const libKeys = [["library.favorite", "加入最愛"], ["library.rename", "重新命名"], ["library.delete", "刪除"]]
    .map(([id, verb]) => [keyLabel(id), verb]).filter(([k]) => k).map(([k, verb]) => `${k} ${verb}`).join("，");
  const pickFiles = (e) => {
    const files = [...e.currentTarget.files];
    e.currentTarget.value = "";
    if (files.length) uploadFiles(files, { target: "library" });
  };
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  const drop = {
    onDragOver: (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      if (!dropping) setDropping(true);
    },
    onDragLeave: (e) => {
      if (!e.currentTarget.contains(e.relatedTarget)) setDropping(false);
    },
    onDrop: (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setDropping(false);
      uploadFiles([...e.dataTransfer.files], { target: "library" });
    },
  };

  const empty = !all.length && !pending.length;
  return html`<div class="lib-pane">
    <div class="lib-toolbar">
      <div class="chips">
        ${FILTERS.filter((f) => f.id !== "trash" || counts.trash || filter === "trash").map((f) => html`<${Chip} key=${f.id}
          active=${filter === f.id} icon=${f.icon} onClick=${() => setFilter(f.id)}
          title=${f.id === "trash" ? "重新生成後被換下的舊配音，右鍵可換回；按下方「清除」會永久刪除" : undefined}>
          ${f.label}<span class="count">${counts[f.id]}</span><//>`)}
      </div>
      <label class="search-box">
        <${Icon} name="search" size=${14} />
        <input type="search" value=${query} placeholder="搜尋名稱、備註或標籤" aria-label="搜尋素材"
          onInput=${(e) => setQuery(e.currentTarget.value)} />
      </label>
      <${Select} value=${sort} options=${SORTS} onChange=${setSort} title="排序" class="lib-sort" />
      ${allTags.length > 0 && html`<${Select} value=${tag} onChange=${setTag} title="依標籤篩選" class="lib-sort"
        options=${[{ value: "", label: "所有標籤" }, ...allTags.map((t) => ({ value: t, label: `# ${t}` }))]} />`}
      <span class="grow"></span>
      ${filter === "unused" && list.length > 0 && html`<${Button} kind="danger-ghost" icon="trash"
        onClick=${() => removeUnused(list)}>移除這 ${list.length} 個<//>`}
      <input ref=${fileRef} type="file" multiple accept=${ACCEPT_ALL} hidden onChange=${pickFiles} />
      <${Button} kind="primary" icon="upload" onClick=${() => importFiles(fileRef, { target: "library" })}>匯入檔案<//>
    </div>
    ${bulk && html`<div class="lib-bulk" role="toolbar" aria-label="批次操作">
      <b>已選 ${bulk.length} 個</b>
      <${Button} size="sm" kind="ghost" icon="star" onClick=${() => favoriteMany(bulk)}>
        ${bulk.every((m) => m.favorite) ? "取消最愛" : "加入最愛"}<//>
      <${Button} size="sm" kind="ghost" icon="film" title=${`圖片與影片接在最後一個鏡頭後面，聲音從播放頭 ${fmtTime(clock.time)} 開始`}
        onClick=${() => placeMany(project, bulk)}>加到時間軸<//>
      <${Button} size="sm" kind="ghost" icon="hash" onClick=${() => tagMany(bulk)}>加標籤<//>
      <${Button} size="sm" kind="danger-ghost" icon="trash" onClick=${() => removeMany(bulk, uses)}>刪除<//>
      <span class="grow"></span>
      <${Button} size="sm" kind="ghost" icon="x" onClick=${() => setSel(null)}>取消選取<//>
    </div>`}
    <div class=${`lib-scroll ${dropping ? "drop-files" : ""}`} ...${drop}>
      ${empty
        ? html`<${Empty} icon="images" title="素材庫是空的" class="lib-empty"
            actions=${html`<${Button} kind="primary" icon="upload" onClick=${() => importFiles(fileRef, { target: "library" })}>匯入檔案<//>`}>
            把圖片、影片或音樂拖進這裡。圖片和影片可以設為鏡頭畫面或參考圖，音樂可以放到剪輯頁的音軌。
          <//>`
        : !list.length && !pending.length
          ? html`<${Empty} icon="filter" title="沒有符合的素材"
              actions=${html`<${Button} onClick=${() => { setFilter("all"); setQuery(""); }}>清除篩選<//>`} />`
          : html`<div class="lib-grid" role="listbox" aria-label="素材" aria-multiselectable="true" ref=${gridRef}
              onClick=${(e) => { if (e.target === e.currentTarget) setSel(null); }}>
              ${pending.map((j) => html`<${PendingCard} key=${j.id} job=${j} />`)}
              ${list.map((m) => html`<${MediaCard} key=${m.id} media=${m} project=${project} selected=${picked.has(m.id)}
                missing=${missing.has(m.id)} uses=${uses} onSelect=${select} onOpen=${preview} onMenu=${(e, x) => mediaMenu(e, ctx, x)} />`)}
            </div>`}
      ${dropping && html`<div class="drop-hint"><${Icon} name="upload" size=${28} /><span>放開以匯入素材庫</span></div>`}
    </div>
    <div class="lib-foot">
      <span class="dim">雙擊預覽，右鍵開啟選單，Ctrl 或 Shift 加選${libKeys ? `；${libKeys}` : ""}</span>
      <span class="grow"></span>
      <${UsageBar} pid=${pid} rev=${project.rev} />
    </div>
  </div>`;
}

export function LibraryView() {
  const { project, tab, jobs } = useStore((s) => ({ project: s.project, tab: s.libraryTab, jobs: s.jobs }));
  if (!project) {
    return html`<div class="view view-library view-empty">
      <${Empty} icon="images" title="還沒有開啟專案"
        actions=${html`<${Button} kind="primary" icon="home" onClick=${() => navigate("home")}>回到專案列表<//>`}>
        素材庫屬於每個專案，開啟專案後就能匯入圖片、影片與音樂，也能管理輸出的成品。
      <//>
    </div>`;
  }
  const busyExports = jobs.filter((j) => j.pid === project.id && (j.kind === "export" || j.kind === "package")
    && (j.status === "queued" || j.status === "running")).length;
  const tabs = [
    { id: "media", label: "素材", icon: "images", badge: Object.values(project.media || {}).filter((m) => !m.trashed).length },
    { id: "exports", label: "輸出成品", icon: "package", badge: busyExports ? `${busyExports} 進行中` : null },
  ];
  return html`<div class="view view-library">
    <header class="lib-head">
      <div class="lib-title">
        <h2>素材庫</h2>
        <${Badge} tone="neutral">${project.name}<//>
      </div>
      <${Tabs} value=${tab} tabs=${tabs} onChange=${(id) => setState({ libraryTab: id })} />
      <span class="grow"></span>
      <${IconButton} icon="folder-open" title="開啟專案資料夾" onClick=${() => revealInFolder("")} />
    </header>
    ${tab === "exports" ? html`<${ExportsPane} project=${project} />` : html`<${MediaPane} project=${project} />`}
  </div>`;
}
