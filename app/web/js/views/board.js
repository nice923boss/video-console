// Storyboard view: every shot as a card in story order. Filter, multi-select (Ctrl / Shift), drag
// cards to reorder, drop image or video files onto a card to make them that shot's picture, and edit
// the focused shot in the inspector on the right.
import { html, useMemo, useRef, useState, useEffect } from "../lib/h.js";
import { useStore, setState, getState } from "../lib/store.js";
import { addShot, updateShots, deleteShots, duplicateShots, moveShot, focusShot, navigate } from "../lib/actions.js";
import { generate, uploadFiles, activeJobsFor } from "../lib/jobs-actions.js";
import { buildSequence, sequenceDuration, shotCover, imageTake, videoTake } from "../lib/sequence.js";
import { fmtDuration, fmtSeconds, shotLabel, shotName } from "../lib/format.js";
import { useKeymap, keyHint, keyLabel } from "../lib/keymap.js";
import { usePref } from "../lib/prefs.js";
import { Icon } from "../components/icons.js";
import { Button, IconButton, Chip, Badge, Progress, Spinner, Empty, Segmented } from "../components/ui.js";
import { Thumb, mediaEntry, openLightbox } from "../components/media.js";
import { openMenu, promptDialog } from "../components/overlay.js";
import { toast } from "../components/toast.js";
import { Inspector } from "../components/inspector.js";
import { openScriptImport } from "../dialogs/script-import.js";

const DRAG_TYPE = "application/x-vc-shot";
const DENSITIES = [
  { value: "sm", icon: "grid", title: "小卡片" },
  { value: "md", icon: "panel", title: "中卡片" },
  { value: "lg", icon: "rows", title: "大卡片" },
];
const FILTERS = [
  { id: "all", label: "全部", test: () => true },
  { id: "noImage", label: "缺圖片", icon: "image", test: (x) => !x.image },
  { id: "noVideo", label: "缺影片", icon: "film", test: (x) => !x.video },
  { id: "busy", label: "生成中", icon: "activity", test: (x) => x.busy.length > 0 },
  { id: "off", label: "不在序列", icon: "eye-off", test: (x) => x.shot.inSequence === false },
];
const NO_JOBS = [];

const hasType = (e, type) => !!e.dataTransfer && [...e.dataTransfer.types].includes(type);

// Index the shot should get in project.shots when dropped before or after the target card.
function dropIndex(shots, id, targetId, side) {
  const i = shots.findIndex((x) => x.id === id);
  const j = shots.findIndex((x) => x.id === targetId);
  if (i < 0 || j < 0) return -1;
  return side === "before" ? (i < j ? j - 1 : j) : (i <= j ? j : j + 1);
}

function previewTake(project, shot) {
  return videoTake(project, shot) || imageTake(project, shot);
}

function openShotPreview(pid, project, rows, id) {
  const items = [];
  let start = -1;
  for (const row of rows) {
    const take = previewTake(project, row.shot);
    if (!take) continue;
    if (row.shot.id === id) start = items.length;
    items.push(mediaEntry(take, { name: shotLabel(row.shot, row.index) }));
  }
  if (start < 0) {
    focusShot(id, { tab: "generate" });
    toast("這個鏡頭還沒有圖片或影片，可以在右側「生成」分頁產生", { duration: 3000 });
    return;
  }
  openLightbox(pid, items, start);
}

async function moveShotTo(shot, index, count) {
  const text = await promptDialog({
    title: `移動「${shotLabel(shot, index)}」`,
    label: `移到第幾個（1 到 ${count}）`,
    value: String(index + 1),
    confirmLabel: "移動",
    validate: (v) => (/^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= count ? "" : `請輸入 1 到 ${count} 的整數`),
  });
  if (text == null) return;
  const to = Number(text) - 1;
  if (to !== index) moveShot(shot.id, to);
}

function shotMenu(e, ctx, row) {
  const { project, selected, rows, pid } = ctx;
  const { shot, index } = row;
  const ids = selected.length > 1 && selected.includes(shot.id) ? selected : [shot.id];
  const single = ids.length === 1;
  if (single && !selected.includes(shot.id)) focusShot(shot.id);
  const last = project.shots.length - 1;
  const allOff = ids.every((id) => (project.shots.find((x) => x.id === id) || {}).inSequence === false);
  openMenu(e, [
    { header: single ? shotLabel(shot, index) : `已選 ${ids.length} 個鏡頭` },
    { label: "生成圖片", icon: "image", hint: keyLabel("board.image"), onClick: () => generate("image", ids) },
    { label: "生成影片", icon: "video", hint: keyLabel("board.video"), onClick: () => generate("video", ids) },
    { divider: true },
    single && { label: "預覽畫面", icon: "eye", hint: keyLabel("board.preview"), disabled: !previewTake(project, shot),
      onClick: () => openShotPreview(pid, project, rows, shot.id) },
    single && { label: "編輯描述", icon: "pencil", onClick: () => focusShot(shot.id, { tab: "content" }) },
    single && { label: "在剪輯頁查看", icon: "scissors", onClick: () => focusShot(shot.id, { view: "edit" }) },
    single && { divider: true },
    single && { label: "在前面插入空白鏡頭", icon: "plus", onClick: () => addShot({ index }) },
    single && { label: "在後面插入空白鏡頭", icon: "plus", onClick: () => addShot({ index: index + 1 }) },
    { label: single ? "複製鏡頭" : `複製 ${ids.length} 個鏡頭`, icon: "copy", hint: keyLabel("board.duplicate"),
      onClick: () => duplicateShots(ids) },
    single && { label: "移到最前面", icon: "to-top", disabled: index === 0, onClick: () => moveShot(shot.id, 0) },
    single && { label: "移到最後面", icon: "arrow-right", disabled: index === last, onClick: () => moveShot(shot.id, last) },
    single && { label: "移到第幾個…", icon: "hash", disabled: last === 0, onClick: () => moveShotTo(shot, index, last + 1) },
    { label: allOff ? "放入輸出序列" : "移出輸出序列", icon: allOff ? "eye" : "eye-off",
      onClick: () => updateShots(ids, { inSequence: allOff }, allOff ? "放入序列" : "移出序列") },
    { divider: true },
    { label: single ? "刪除鏡頭" : `刪除 ${ids.length} 個鏡頭`, icon: "trash", danger: true, hint: keyLabel("board.delete"),
      onClick: () => deleteShots(ids) },
  ]);
}

function CardStatus({ busy }) {
  const running = busy.find((j) => j.status === "running");
  return html`<div class="sc-busy">
    <${Spinner} size=${13} />
    <span>${running ? `生成中 ${Math.round((running.progress || 0) * 100)}%` : "排隊中"}${busy.length > 1 ? `（${busy.length}）` : ""}</span>
    ${running && html`<${Progress} value=${running.progress || 0} />`}
  </div>`;
}

function ShotCard({ row, ctx, drop, handlers }) {
  const { project, pid, routeColors, ratio } = ctx;
  const { shot, index, image, video, busy, length, selected, focused, caption } = row;
  const cover = shotCover(project, shot);
  const running = busy.find((j) => j.status === "running" && j.previewRev);
  const off = shot.inSequence === false;
  const dropCls = drop && drop.id === shot.id ? `drop-${drop.side}` : "";
  const cls = ["shot-card", selected && "selected", focused && "focused", off && "is-off", dropCls].filter(Boolean).join(" ");
  const style = [`--route:${routeColors[shot.route] || "var(--muted)"}`, shot.color ? `--shot-color:${shot.color}` : ""]
    .filter(Boolean).join(";");
  return html`<div class=${cls} style=${style} data-shot=${shot.id} tabindex="0" draggable="true"
      role="option" aria-selected=${selected ? "true" : "false"} aria-label=${shotLabel(shot, index)}
      onClick=${(e) => handlers.click(e, row)} onDblClick=${() => handlers.preview(row)}
      onContextMenu=${(e) => shotMenu(e, ctx, row)}
      onKeyDown=${(e) => { if (e.key === "Enter" && e.target === e.currentTarget) focusShot(shot.id, { tab: "content" }); }}
      onDragStart=${(e) => handlers.dragStart(e, row)} onDragEnd=${handlers.dragEnd}
      onDragOver=${(e) => handlers.dragOver(e, row)} onDragLeave=${(e) => handlers.dragLeave(e, row)}
      onDrop=${(e) => handlers.drop(e, row)}>
    <button type="button" class="sc-insert" title="在這裡插入空白鏡頭" aria-label=${`在${shotLabel(shot, index)}前面插入空白鏡頭`}
      onClick=${(e) => { e.stopPropagation(); addShot({ index }); }} onDblClick=${(e) => e.stopPropagation()}><i></i></button>
    <div class="sc-media">
      <${Thumb} pid=${pid} path=${running ? "" : cover ? cover.path : ""} ratio=${ratio} icon="clapper"
          src=${running ? `/api/jobs/${encodeURIComponent(running.id)}/preview?v=${running.previewRev}` : ""}
          strip=${video && video.strip ? video.strip : ""} video=${video ? video.file : ""}>
        <span class="sc-num mono">${index + 1}</span>
        <span class="sc-flags">
          <i class=${image ? "on" : ""} title=${image ? "已有圖片" : "還沒有圖片"}><${Icon} name="image" size=${12} /></i>
          <i class=${video ? "on" : ""} title=${video ? "已有影片" : "還沒有影片"}><${Icon} name="film" size=${12} /></i>
        </span>
        <span class="sc-dur mono">${fmtSeconds(length)}</span>
        ${off && html`<span class="sc-off"><${Icon} name="eye-off" size=${12} />不在序列</span>`}
        ${busy.length > 0 && html`<${CardStatus} busy=${busy} />`}
        ${drop && drop.id === shot.id && drop.side === "files" && html`<div class="sc-dropfile">
          <${Icon} name="upload" size=${20} /><span>放開後加入這個鏡頭</span></div>`}
      <//>
    </div>
    <div class="sc-body">
      <div class="sc-row">
        <i class="route-dot" title=${`路由 ${shot.route}`}></i>
        <b class="sc-name" title=${shotName(shot, index)}>${shotName(shot, index)}</b>
        <${IconButton} icon="more" size=${15} title="更多動作" class="sc-more" onClick=${(e) => shotMenu(e, ctx, row)} />
      </div>
      <p class=${`sc-prompt ${shot.prompt ? "" : "dim"}`}>${shot.prompt || "尚未填寫畫面描述"}</p>
      ${caption && (caption.text || "").trim() && html`<p class="sc-caption" title=${`字幕：${caption.text}`}>
        <${Icon} name="captions" size=${12} /><span>${caption.text}</span></p>`}
      ${(shot.camera || shot.motion) && html`<div class="sc-meta">
        ${shot.camera && html`<${Badge} tone="blue"><${Icon} name="camera" size=${11} />${shot.camera}<//>`}
        ${shot.motion && html`<span class="dim" title=${shot.motion}><${Icon} name="move" size=${11} />${shot.motion}</span>`}
      </div>`}
    </div>
  </div>`;
}

function Toolbar({ project, stats, filter, setFilter, density, setDensity, total }) {
  return html`<div class="board-toolbar">
    <div class="bt-title">
      <h2 title=${project.name}>${project.name}</h2>
      <span class="dim">${project.shots.length} 個鏡頭<i></i>序列 ${fmtDuration(total)}</span>
    </div>
    <div class="chips" role="group" aria-label="篩選鏡頭">
      ${FILTERS.map((f) => html`<${Chip} key=${f.id} icon=${f.icon} active=${filter === f.id}
          disabled=${f.id !== "all" && !stats[f.id] && filter !== f.id} onClick=${() => setFilter(f.id)}>
        ${f.label}${f.id === "all" ? "" : ` ${stats[f.id]}`}
      <//>`)}
    </div>
    <span class="grow"></span>
    <${Segmented} size="sm" value=${density} onChange=${setDensity}
      options=${DENSITIES.map((d) => ({ ...d, label: "" }))} />
    <${Button} icon="book" onClick=${openScriptImport}>匯入劇本<//>
    <${Button} kind="primary" icon="plus" title=${`插在目前鏡頭之後${keyHint("board.add")}`} onClick=${() => addShot()}>新增鏡頭<//>
  </div>`;
}

function SelectionBar({ project, ids, clear }) {
  const allOff = ids.every((id) => (project.shots.find((x) => x.id === id) || {}).inSequence === false);
  return html`<div class="board-selbar" role="toolbar" aria-label="多選動作">
    <b>已選 ${ids.length} 個鏡頭</b>
    <${Button} size="sm" icon="image" onClick=${() => generate("image", ids)}>生成圖片<//>
    <${Button} size="sm" icon="video" onClick=${() => generate("video", ids)}>生成影片<//>
    <${Button} size="sm" icon="copy" onClick=${() => duplicateShots(ids)}>複製<//>
    <${Button} size="sm" icon=${allOff ? "eye" : "eye-off"}
      onClick=${() => updateShots(ids, { inSequence: allOff }, allOff ? "放入序列" : "移出序列")}>
      ${allOff ? "放入序列" : "移出序列"}<//>
    <${Button} size="sm" kind="danger" icon="trash" onClick=${() => deleteShots(ids)}>刪除<//>
    <span class="grow"></span>
    <${Button} size="sm" kind="ghost" icon="x" onClick=${clear}>取消選取<//>
  </div>`;
}

export function BoardView() {
  const { project, pid, jobs, selected, focus, settings } = useStore((s) => ({
    project: s.project, pid: s.pid, jobs: s.jobs, selected: s.selected, focus: s.focus, settings: s.settings,
  }));
  const [filter, setFilter] = useState("all");
  const [density, setDensity] = usePref("vc.board.density", "md", (v) => DENSITIES.some((d) => d.value === v));
  const [drop, setDrop] = useState(null);
  const [gridDrop, setGridDrop] = useState(false);
  const gridRef = useRef(null);
  const dragRef = useRef(null);
  const anchorRef = useRef(null);

  const clips = useMemo(() => (project ? buildSequence(project, true) : []), [project]);
  const total = useMemo(() => (project ? sequenceDuration(buildSequence(project)) : 0), [project]);
  const rows = useMemo(() => {
    if (!project) return [];
    const lengths = new Map(clips.map((c) => [c.shot.id, c.length]));
    const captions = new Map((project.captions || []).filter((c) => c.shot).map((c) => [c.shot, c]));
    return project.shots.map((shot, index) => ({
      shot,
      index,
      image: imageTake(project, shot),
      video: videoTake(project, shot),
      busy: jobs.length ? activeJobsFor(jobs, pid, shot.id) : NO_JOBS,
      length: lengths.get(shot.id) || shot.duration || 0,
      selected: selected.includes(shot.id),
      focused: focus === shot.id,
      caption: captions.get(shot.id) || null,
    }));
  }, [project, clips, jobs, pid, selected, focus]);
  const stats = useMemo(() => Object.fromEntries(FILTERS.map((f) => [f.id, rows.filter(f.test).length])), [rows]);
  const active = FILTERS.find((f) => f.id === filter) || FILTERS[0];
  const visible = useMemo(() => rows.filter(active.test), [rows, active]);

  // A filter that empties out (the last missing picture got generated) falls back to everything.
  useEffect(() => {
    if (filter !== "all" && !stats[filter]) setFilter("all");
  }, [filter, stats]);

  useEffect(() => {
    if (!focus || !gridRef.current) return;
    const el = gridRef.current.querySelector(`[data-shot="${CSS.escape(focus)}"]`);
    if (el) el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [focus]);

  const routeColors = useMemo(() => Object.fromEntries(((settings && settings.routes) || []).map((r) => [r.id, r.color])),
    [settings]);
  const ratio = project ? `${project.settings.width}/${project.settings.height}` : "16/9";
  const ctx = { project, pid, selected, rows: visible, routeColors, ratio };

  const selectAll = () => setState({ selected: visible.map((r) => r.shot.id), focus: focus || (visible[0] && visible[0].shot.id) });
  const clearSelection = () => setState((s) => ({ selected: s.focus ? [s.focus] : [] }));
  const targets = () => {
    const s = getState();
    if (s.selected.length) return s.selected;
    return s.focus ? [s.focus] : [];
  };

  const columns = () => {
    const grid = gridRef.current;
    if (!grid) return 1;
    return Math.max(1, getComputedStyle(grid).gridTemplateColumns.split(" ").filter(Boolean).length);
  };
  const step = (delta) => {
    if (!visible.length) return;
    const at = visible.findIndex((r) => r.shot.id === focus);
    const next = at < 0 ? 0 : Math.max(0, Math.min(visible.length - 1, at + delta));
    anchorRef.current = visible[next].shot.id;
    focusShot(visible[next].shot.id);
  };
  const nudge = (delta) => {
    if (!focus || !project) return;
    const i = project.shots.findIndex((x) => x.id === focus);
    const j = i + delta;
    if (i >= 0 && j >= 0 && j < project.shots.length) moveShot(focus, j);
  };

  useKeymap({
    "board.add": () => addShot(),
    "board.delete": () => deleteShots(targets()),
    "board.duplicate": () => duplicateShots(targets()),
    "board.selectAll": selectAll,
    "board.deselect": () => (getState().selected.length > 1 ? clearSelection() : false),
    "board.left": () => step(-1),
    "board.right": () => step(1),
    "board.up": () => step(-columns()),
    "board.down": () => step(columns()),
    "board.first": () => step(-visible.length),
    "board.last": () => step(visible.length),
    "board.moveBack": () => nudge(-1),
    "board.moveForward": () => nudge(1),
    "board.image": () => { const ids = targets(); if (ids.length) generate("image", ids); },
    "board.video": () => { const ids = targets(); if (ids.length) generate("video", ids); },
    "board.preview": () => { if (focus) openShotPreview(pid, project, visible, focus); },
  }, { active: !!project });

  const handlers = {
    click(e, row) {
      const id = row.shot.id;
      if (e.target.closest && e.target.closest(".sc-more")) return;
      if (e.shiftKey) {
        const from = visible.findIndex((r) => r.shot.id === (anchorRef.current || focus));
        const to = visible.findIndex((r) => r.shot.id === id);
        if (from >= 0 && to >= 0) {
          const [a, b] = from < to ? [from, to] : [to, from];
          setState({ selected: visible.slice(a, b + 1).map((r) => r.shot.id), focus: id });
          return;
        }
      }
      anchorRef.current = id;
      if (e.ctrlKey || e.metaKey) {
        setState((s) => {
          const has = s.selected.includes(id);
          const next = has ? s.selected.filter((x) => x !== id) : [...s.selected, id];
          return { selected: next, focus: has ? next[next.length - 1] || null : id };
        });
        return;
      }
      focusShot(id);
    },
    preview(row) {
      openShotPreview(pid, project, visible, row.shot.id);
    },
    dragStart(e, row) {
      dragRef.current = row.shot.id;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData(DRAG_TYPE, row.shot.id);
    },
    dragEnd() {
      dragRef.current = null;
      setDrop(null);
    },
    dragOver(e, row) {
      if (hasType(e, DRAG_TYPE) || dragRef.current) {
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = "move";
        const r = e.currentTarget.getBoundingClientRect();
        const side = e.clientX < r.left + r.width / 2 ? "before" : "after";
        if (!drop || drop.id !== row.shot.id || drop.side !== side) setDrop({ id: row.shot.id, side });
      } else if (hasType(e, "Files")) {
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = "copy";
        setGridDrop(false);
        if (!drop || drop.id !== row.shot.id || drop.side !== "files") setDrop({ id: row.shot.id, side: "files" });
      }
    },
    dragLeave(e, row) {
      if (e.currentTarget.contains(e.relatedTarget)) return;
      setDrop((d) => (d && d.id === row.shot.id ? null : d));
    },
    drop(e, row) {
      e.preventDefault();
      e.stopPropagation();
      const side = drop && drop.id === row.shot.id ? drop.side : null;
      setDrop(null);
      const dragged = e.dataTransfer.getData(DRAG_TYPE) || dragRef.current;
      dragRef.current = null;
      if (dragged) {
        if (dragged === row.shot.id || !side || side === "files") return;
        const at = dropIndex(project.shots, dragged, row.shot.id, side);
        const from = project.shots.findIndex((x) => x.id === dragged);
        if (at >= 0 && at !== from) moveShot(dragged, at);
        return;
      }
      const files = [...(e.dataTransfer.files || [])];
      if (files.length) {
        focusShot(row.shot.id, { tab: "takes" });
        uploadFiles(files, { target: "shot", shot: row.shot.id });
      }
    },
  };

  const gridDragOver = (e) => {
    if (!hasType(e, "Files") || dragRef.current) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    if (!gridDrop) setGridDrop(true);
  };
  const gridDrop_ = (e) => {
    setGridDrop(false);
    if (!hasType(e, "Files") || dragRef.current) return;
    e.preventDefault();
    const files = [...(e.dataTransfer.files || [])];
    if (files.length) {
      uploadFiles(files, { target: "library" });
      if (files.some((f) => !/\.zip$/i.test(f.name))) toast("檔案會放進素材庫；拖到某個鏡頭卡片上可以直接指定給它", { duration: 3600 });
    }
  };

  if (!project) {
    return html`<div class="view view-board view-empty">
      <${Empty} icon="clapper" title="還沒有開啟專案"
        actions=${html`<${Button} kind="primary" icon="home" onClick=${() => navigate("home")}>回到專案列表<//>`}>
        在首頁開啟或建立一個專案後，就能在這裡排分鏡。
      <//>
    </div>`;
  }

  let body;
  if (!project.shots.length) {
    body = html`<${Empty} icon="clapper" title="還沒有鏡頭" class="board-empty"
        actions=${html`<${Button} kind="primary" icon="plus" onClick=${() => addShot()}>新增鏡頭<//>
          <${Button} icon="book" onClick=${openScriptImport}>從劇本建立<//>`}>
      一個鏡頭就是一個畫面：先寫描述生成圖片，再讓圖片動起來。也可以貼上整份劇本自動拆成鏡頭。
    <//>`;
  } else if (!visible.length) {
    body = html`<${Empty} icon="filter" title="沒有符合篩選的鏡頭" class="board-empty"
      actions=${html`<${Button} onClick=${() => setFilter("all")}>顯示全部<//>`} />`;
  } else {
    body = html`<div class=${`board-grid density-${density}`} ref=${gridRef} role="listbox" aria-multiselectable="true"
        aria-label="分鏡">
      ${visible.map((row) => html`<${ShotCard} key=${row.shot.id} row=${row} ctx=${ctx} drop=${drop} handlers=${handlers} />`)}
      ${filter === "all" && html`<button type="button" class="shot-new" title="在最後面新增鏡頭"
          onClick=${() => addShot({ index: project.shots.length })}>
        <span class="shot-new-icon"><${Icon} name="plus" size=${22} /></span>
        <span>新增鏡頭</span>
      </button>`}
    </div>`;
  }

  return html`<div class="view view-board">
    <section class="board-main">
      <${Toolbar} project=${project} stats=${stats} filter=${filter} setFilter=${setFilter}
        density=${density} setDensity=${setDensity} total=${total} />
      ${selected.length > 1 && html`<${SelectionBar} project=${project} ids=${selected} clear=${clearSelection} />`}
      <div class=${`board-scroll ${gridDrop ? "drop-files" : ""}`} onDragOver=${gridDragOver}
          onDragLeave=${(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setGridDrop(false); }} onDrop=${gridDrop_}
          onClick=${(e) => { if (e.target === e.currentTarget && getState().selected.length > 1) clearSelection(); }}>
        ${body}
      </div>
      <div class="board-hint dim">
        <span>拖曳卡片調整順序</span><i></i><span>Ctrl / Shift 多選</span><i></i>
        <span>G 生圖，V 生影片，Space 預覽</span><i></i><span>把圖片或影片拖到卡片上直接使用</span><i></i>
        <span>點卡片左側的 + 插入空白鏡頭</span>
      </div>
    </section>
    <${Inspector} />
  </div>`;
}
