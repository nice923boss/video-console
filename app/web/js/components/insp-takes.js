// Shot inspector, "版本" tab: every image and video generated or imported for the shot. Pick the one
// the shot uses, rate and label them, and branch new work (video, upscale, last frame) from any take.
// Two takes can be set side by side (SB-12).
import { html, useState, useMemo, useRef } from "../lib/h.js";
import { setState, useStore, selectProject } from "../lib/store.js";
import { useDialogKeys, confirmKeyText } from "../lib/keymap.js";
import { updateShot, ops, undo, revealInFolder } from "../lib/actions.js";
import { generate, cancelJob } from "../lib/jobs-actions.js";
import { fmtDuration, fmtRelTime, fileUrl, KIND_LABEL } from "../lib/format.js";
import { Icon } from "./icons.js";
import { Segmented, Button, IconButton, Empty, Spinner, Progress, Select } from "./ui.js";
import { Thumb, mediaEntry, openLightbox } from "./media.js";
import { openMenu, promptDialog, openModal } from "./overlay.js";
import { toast } from "./toast.js";

const STARS = [1, 2, 3, 4, 5];
const ACTIVE_KEY = { image: "activeImage", video: "activeVideo" };

const updateTake = (id, patch, label) => ops([{ op: "take.update", id, patch }], { label });

// "圖片 3" / "影片 2", numbered per kind in creation order, unless the take has its own label.
function takeNames(takes) {
  const seen = { image: 0, video: 0 };
  const names = new Map();
  for (const t of [...takes].sort((a, b) => (a.created || 0) - (b.created || 0))) {
    seen[t.kind] = (seen[t.kind] || 0) + 1;
    names.set(t.id, t.label || `${t.kind === "video" ? "影片" : "圖片"} ${seen[t.kind]}`);
  }
  return names;
}

function Stars({ value, onChange }) {
  return html`<span class="stars" role="radiogroup" aria-label="評分">
    ${STARS.map((n) => html`<button type="button" key=${n} class=${`star ${n <= value ? "on" : ""}`}
        title=${n === value ? "取消評分" : `${n} 顆星`} aria-label=${`${n} 顆星`} onClick=${() => onChange(n === value ? 0 : n)}>
      <${Icon} name="star" size=${12} />
    </button>`)}
  </span>`;
}

function useTake(shot, take) {
  const key = ACTIVE_KEY[take.kind];
  return () => updateShot(shot.id, { [key]: take.id }, { label: take.kind === "video" ? "切換使用中的影片" : "切換使用中的圖片" });
}

async function rename(take, name) {
  const next = await promptDialog({
    title: "重新命名版本", label: "版本名稱", value: take.label || "", placeholder: name,
    hint: "留白會恢復成自動編號", confirmLabel: "儲存", validate: () => "",
  });
  if (next != null && next !== (take.label || "")) updateTake(take.id, { label: next.slice(0, 120) }, "重新命名版本");
}

async function editNote(take) {
  const next = await promptDialog({
    title: "版本備註", label: "備註", value: take.note || "", multiline: true, confirmLabel: "儲存", validate: () => "",
    hint: confirmKeyText("儲存"),
  });
  if (next != null && next !== (take.note || "")) updateTake(take.id, { note: next.slice(0, 1000) }, "修改版本備註");
}

async function removeTake(take, name) {
  const res = await ops([{ op: "take.delete", id: take.id }], { label: "刪除版本" });
  if (res) toast(`已刪除「${name}」，檔案仍保留在專案資料夾`, { action: { label: "復原", fn: undo } });
}

// The take to set beside `take`: the one the shot uses for that kind, else the newest other take of the kind,
// else any other take.
function comparePartner(shot, takes, take) {
  const others = takes.filter((t) => t.id !== take.id);
  return others.find((t) => t.id === shot[ACTIVE_KEY[take.kind]]) || others.find((t) => t.kind === take.kind) || others[0];
}

function CompareSide({ pid, shot, take, takes, names, mediaRef, onPick }) {
  const video = take.kind === "video";
  const url = fileUrl(pid, take.file);
  const meta = [
    take.width && `${take.width} × ${take.height}`,
    video && take.duration > 0 && fmtDuration(take.duration),
    take.seed != null && `種子 ${take.seed}`,
  ].filter(Boolean).join("　");
  return html`<div class="cmp-side">
    <div class="cmp-head">
      <${Select} value=${take.id} onChange=${onPick} title="換一個版本"
        options=${takes.map((t) => ({ value: t.id, label: names.get(t.id) }))} />
      ${shot[ACTIVE_KEY[take.kind]] === take.id
        ? html`<span class="cmp-active"><${Icon} name="check" size=${12} />使用中</span>`
        : html`<${Button} size="sm" kind="ghost" onClick=${useTake(shot, take)}>使用<//>`}
    </div>
    <div class="cmp-media">
      ${video
        ? html`<video ref=${mediaRef} src=${url} controls loop playsinline key=${take.id}></video>`
        : html`<img src=${url} alt=${names.get(take.id)} draggable="false" key=${take.id} />`}
    </div>
    <div class="cmp-foot">
      <span class="dim mono">${meta}</span>
      <${Stars} value=${take.rating || 0} onChange=${(rating) => updateTake(take.id, { rating }, "評分版本")} />
    </div>
  </div>`;
}

// Reads the live project, so a take used or rated here shows at once and a deleted one drops out.
function TakeCompare({ shotId, first, second, close }) {
  const project = useStore(selectProject);
  const [ids, setIds] = useState([first, second]);
  const left = useRef(null);
  const right = useRef(null);
  useDialogKeys({ "dialog.close": { fn: () => close(), allowInInput: true } }, { modal: true });
  const takes = useMemo(() => Object.values((project && project.takes) || {})
    .filter((t) => t.shot === shotId).sort((a, b) => (b.created || 0) - (a.created || 0)), [project && project.takes, shotId]);
  const names = useMemo(() => takeNames(takes), [takes]);
  const shot = project && project.shots.find((s) => s.id === shotId);
  const pair = ids.map((id) => takes.find((t) => t.id === id));
  if (!shot || takes.length < 2 || pair.some((t) => !t)) {
    queueMicrotask(() => close());
    return null;
  }
  const videos = () => [left.current, right.current].filter(Boolean);
  // Both from the first frame, so a timing difference between the two shows.
  const playBoth = () => videos().forEach((v) => {
    v.currentTime = 0;
    v.play().catch(() => {});
  });
  const pick = (side) => (id) => setIds(side ? [ids[0], id] : [id, ids[1]]);
  return html`<div class="lightbox take-compare" role="dialog" aria-modal="true" aria-label="並排比較版本">
    <header class="lb-top">
      <span class="lb-kind"><${Icon} name="columns" size=${16} /></span>
      <span class="lb-title">並排比較版本</span>
      <span class="lb-spacer"></span>
      ${pair.every((t) => t.kind === "video") && html`
        <${Button} size="sm" kind="ghost" icon="play" title="兩段影片都從頭開始播" onClick=${playBoth}>從頭一起播<//>
        <${Button} size="sm" kind="ghost" icon="pause" onClick=${() => videos().forEach((v) => v.pause())}>暫停<//>`}
      <${Button} size="sm" kind="ghost" icon="swap" onClick=${() => setIds([ids[1], ids[0]])}>左右對調<//>
      <button type="button" class="icon-btn" title="關閉（Esc）" aria-label="關閉" onClick=${() => close()}>
        <${Icon} name="x" />
      </button>
    </header>
    <div class="cmp-stage">
      ${pair.map((t, side) => html`<${CompareSide} key=${side} pid=${project.id} shot=${shot} take=${t} takes=${takes}
        names=${names} mediaRef=${side ? right : left} onPick=${pick(side)} />`)}
    </div>
  </div>`;
}

const openCompare = (shot, first, second) => openModal((close) => html`<${TakeCompare} shotId=${shot.id}
  first=${first.id} second=${second.id} close=${close} />`);

function takeMenu(e, { shot, take, name, active, isLast, takes }) {
  const video = take.kind === "video";
  openMenu(e, [
    { header: name },
    !active && { label: video ? "設為使用中的影片" : "設為使用中的圖片", icon: "check", onClick: useTake(shot, take) },
    takes.length > 1 && { label: "並排比較…", icon: "columns", onClick: () => openCompare(shot, take, comparePartner(shot, takes, take)) },
    { label: take.favorite ? "取消收藏" : "收藏", icon: "heart", onClick: () => updateTake(take.id, { favorite: !take.favorite }, "收藏版本") },
    { label: "重新命名…", icon: "pencil", onClick: () => rename(take, name) },
    { label: take.note ? "編輯備註…" : "加上備註…", icon: "clipboard", onClick: () => editNote(take) },
    { divider: true },
    !video && { label: "用這張圖生影片", icon: "film", onClick: () => generate("video", [shot.id], { payload: { image: take.id, useImage: true } }) },
    !video && { label: "放大這張圖", icon: "zoom-in", onClick: () => generate("upscale", [shot.id], { payload: { source: take.id } }) },
    !video && shot.ref !== take.id && {
      label: "設為參考圖", icon: "link", onClick: () => updateShot(shot.id, { ref: take.id }, { label: "設定參考圖" }),
    },
    video && Math.min(take.width || 0, take.height || 0) < 1080 && {
      label: "用 Video2X 放大到 1080P", icon: "zoom-in",
      onClick: () => generate("upscalevideo", [shot.id], { payload: { source: take.id } }),
    },
    video && {
      label: "擷取最後一格到下一鏡", icon: "frame", disabled: isLast, hint: isLast ? "已是最後一鏡" : "",
      onClick: () => generate("lastframe", [shot.id], { payload: { source: take.id, target: "next" } }),
    },
    video && {
      label: "擷取最後一格到這個鏡頭", icon: "frame",
      onClick: () => generate("lastframe", [shot.id], { payload: { source: take.id, target: "self" } }),
    },
    take.seed != null && {
      label: `套用種子 ${take.seed}`, icon: "lock",
      onClick: () => updateShot(shot.id, { seed: take.seed, seedLocked: true }, { label: "套用版本種子" }),
    },
    // A translated take restores the text the user wrote, not the English sent to the model
    (take.promptOriginal || take.prompt) && (take.promptOriginal || take.prompt) !== shot.prompt && {
      label: "還原這個版本的描述", icon: "history",
      onClick: () => updateShot(shot.id, { prompt: take.promptOriginal || take.prompt }, { label: "還原鏡頭描述" }),
    },
    { divider: true },
    { label: "在檔案總管中顯示", icon: "folder-open", onClick: () => revealInFolder(take.file) },
    { label: "刪除版本", icon: "trash", danger: true, onClick: () => removeTake(take, name) },
  ]);
}

function TakeCard({ project, shot, take, takes, name, active, ratio, isLast, onOpen }) {
  const video = take.kind === "video";
  return html`<div class=${`take ${active ? "is-active" : ""}`}>
    <${Thumb} pid=${project.id} path=${take.thumb || take.file} strip=${video ? take.strip || "" : ""}
        video=${video ? take.file : ""} ratio=${ratio} icon=${video ? "film" : "image"} class="take-thumb"
        title="點一下放大檢視" onClick=${onOpen}>
      ${active && html`<span class="take-badge"><${Icon} name="check" size=${11} />使用中</span>`}
      ${shot.ref === take.id && html`<span class="take-badge take-badge-ref"><${Icon} name="link" size=${11} />參考圖</span>`}
      ${video && take.duration > 0 && html`<span class="take-dur mono">${fmtDuration(take.duration)}</span>`}
      ${take.favorite && html`<span class="take-fav"><${Icon} name="heart" size=${12} /></span>`}
    <//>
    <div class="take-meta">
      <span class="take-name" title=${take.note || name}>${name}</span>
      <span class="take-time dim">${fmtRelTime(take.created)}</span>
      <${IconButton} icon="more" size=${15} title="更多動作"
        onClick=${(e) => takeMenu(e, { shot, take, name, active, isLast, takes })} />
    </div>
    <div class="take-foot">
      <${Stars} value=${take.rating || 0} onChange=${(rating) => updateTake(take.id, { rating }, "評分版本")} />
      ${!active && html`<${Button} size="sm" kind="ghost" onClick=${useTake(shot, take)}>使用<//>`}
    </div>
  </div>`;
}

function JobCard({ job, ratio }) {
  const preview = job.previewRev ? `/api/jobs/${encodeURIComponent(job.id)}/preview?v=${job.previewRev}` : "";
  const running = job.status === "running";
  return html`<div class="take take-job">
    <div class="thumb thumb-cover take-thumb take-pending" style=${`aspect-ratio:${ratio}`}>
      ${preview ? html`<img src=${preview} alt="" draggable="false" />` : html`<${Spinner} size=${22} />`}
      <span class="take-badge">${running ? `${Math.round((job.progress || 0) * 100)}%` : "排隊中"}</span>
    </div>
    <div class="take-meta">
      <span class="take-name">${KIND_LABEL[job.kind] || job.kind}</span>
      <${IconButton} icon="x" size=${15} title="取消這個工作" onClick=${() => cancelJob(job.id)} />
    </div>
    <div class="take-foot"><span class="take-msg dim" title=${job.message || ""}>${job.message || (running ? "處理中" : "等待中")}</span></div>
    ${running && html`<${Progress} value=${job.progress || 0} />`}
  </div>`;
}

export function TakesTab({ project, shot, index, takes, busy }) {
  const [filter, setFilter] = useState("all");
  const names = useMemo(() => takeNames(takes), [takes]);
  const count = (fn) => takes.filter(fn).length;
  const filters = [
    { value: "all", label: `全部 ${takes.length}` },
    { value: "image", label: `圖片 ${count((t) => t.kind === "image")}` },
    { value: "video", label: `影片 ${count((t) => t.kind === "video")}` },
    { value: "fav", label: `收藏 ${count((t) => t.favorite)}`, icon: "heart" },
  ];
  const shown = takes.filter((t) => (filter === "all" ? true : filter === "fav" ? t.favorite : t.kind === filter));
  const ratio = `${project.settings.width}/${project.settings.height}`;
  const isLast = index + 1 >= project.shots.length;
  const jobs = (busy || []).filter((j) => ["image", "video", "upscale", "upscalevideo", "kenburns", "lastframe"].includes(j.kind));

  if (!takes.length && !jobs.length) {
    return html`<${Empty} icon="layers" title="還沒有任何版本"
        actions=${html`<${Button} kind="primary" icon="sparkles" onClick=${() => setState({ inspectorTab: "generate" })}>前往生成<//>`}>
      生圖或生影片的結果都會留在這裡，可以隨時切換要用哪一個
    <//>`;
  }
  const entries = shown.map((t) => mediaEntry(t, {
    name: names.get(t.id),
    actions: [{ label: "使用這個版本", icon: "check", fn: (item) => useTake(shot, takes.find((x) => x.id === item.id))() }],
  }));
  // The compare button starts from the take in use within the filter, beside its partner.
  const pool = shown.length > 1 ? shown : takes;
  const base = pool.find((t) => t.id === shot.activeVideo) || pool.find((t) => t.id === shot.activeImage) || pool[0];
  return html`<div class="takes">
    <div class="takes-bar">
      <${Segmented} size="sm" value=${filter} onChange=${setFilter} options=${filters} class="takes-filter" />
      ${takes.length > 1 && html`<${IconButton} icon="columns" size=${16} title="並排比較兩個版本"
        onClick=${() => openCompare(shot, base, comparePartner(shot, pool, base))} />`}
    </div>
    <div class="take-grid">
      ${jobs.map((j) => html`<${JobCard} key=${j.id} job=${j} ratio=${ratio} />`)}
      ${shown.map((t, i) => html`<${TakeCard} key=${t.id} project=${project} shot=${shot} take=${t} takes=${takes} name=${names.get(t.id)}
        active=${shot[ACTIVE_KEY[t.kind]] === t.id} ratio=${ratio} isLast=${isLast}
        onOpen=${() => openLightbox(project.id, entries, i)} />`)}
    </div>
    ${!shown.length && html`<p class="insp-hint">${filter === "fav" ? "還沒有收藏的版本" : "這個分類還沒有版本"}</p>`}
  </div>`;
}
