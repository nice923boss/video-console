// Project home: a hero with quick starts and live status, the project grid and the trash.
import { html, useState, useEffect, useMemo } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { usePref } from "../lib/prefs.js";
import { openProject, duplicateProject, deleteProject, updateProject, navigate, pickProjectZip } from "../lib/actions.js";
import { fmtDuration, fmtRelTime, fmtDate } from "../lib/format.js";
import { comfyTone } from "../components/shell.js";
import { Icon } from "../components/icons.js";
import { Button, IconButton, Segmented, Badge, Dot, Empty, Section, Spinner } from "../components/ui.js";
import { Thumb } from "../components/media.js";
import { openMenu, promptDialog, confirmDialog } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { openNewProject } from "../dialogs/new-project.js";
import { openScriptImport } from "../dialogs/script-import.js";

const enc = encodeURIComponent;
const SORTS = [
  { value: "recent", label: "最近修改" },
  { value: "name", label: "名稱" },
  { value: "created", label: "建立時間" },
];
const SORT_IDS = SORTS.map((s) => s.value);
const byName = new Intl.Collator("zh-Hant", { numeric: true }).compare;
const SORTERS = {
  recent: (a, b) => (b.updated || 0) - (a.updated || 0),
  name: (a, b) => byName(a.name, b.name),
  created: (a, b) => (b.created || 0) - (a.created || 0),
};

async function renameProject(p, currentPid) {
  const name = await promptDialog({
    title: "重新命名專案", label: "專案名稱", value: p.name, confirmLabel: "儲存",
    validate: (v) => (v.trim() ? "" : "名稱不能是空白"),
  });
  if (!name || name === p.name) return;
  if (p.id === currentPid) {
    updateProject({ name }, { label: "重新命名專案" });
    return;
  }
  try {
    await api.post(`/api/projects/${enc(p.id)}/ops`, { ops: [{ op: "project.update", patch: { name } }], undoable: false });
  } catch (err) {
    toastError(err, "無法重新命名");
  }
}

function projectMenu(e, p, currentPid) {
  e.preventDefault();
  e.stopPropagation();
  openMenu(e, [
    { label: "開啟", icon: "folder-open", onClick: () => openProject(p.id) },
    { label: "開啟剪輯頁", icon: "scissors", onClick: () => openProject(p.id, "edit") },
    { divider: true },
    { label: "重新命名…", icon: "pencil", onClick: () => renameProject(p, currentPid) },
    { label: "建立副本", icon: "copy", onClick: () => duplicateProject(p.id) },
    { divider: true },
    { label: "移到垃圾桶", icon: "trash", danger: true, onClick: () => deleteProject(p.id, p.name) },
  ]);
}

function startFromScript() {
  openNewProject().then((p) => {
    if (p) openScriptImport();
  });
}

function Stat({ icon, label, value, sub, tone = null, onClick }) {
  return html`<button type="button" class=${`stat ${tone ? `stat-${tone}` : ""}`} onClick=${onClick}>
    <span class="stat-icon"><${Icon} name=${icon} size=${18} /></span>
    <span class="stat-body">
      <span class="stat-label">${label}</span>
      <span class="stat-value">${value}</span>
      ${sub && html`<span class="stat-sub">${sub}</span>`}
    </span>
  </button>`;
}

function Hero({ projects, jobs, comfy, lastProject }) {
  const shots = projects.reduce((n, p) => n + (p.shots || 0), 0);
  const running = jobs.filter((j) => j.status === "running").length;
  const queued = jobs.filter((j) => j.status === "queued").length;
  const enabled = comfy.filter((c) => c.enabled !== false);
  const online = enabled.filter((c) => c.online);
  const tone = comfyTone(comfy);
  const gpu = online.find((c) => c.device && c.device.name);
  const hour = new Date().getHours();
  const greet = hour < 5 ? "夜深了" : hour < 11 ? "早安" : hour < 14 ? "午安" : hour < 18 ? "下午好" : "晚上好";
  return html`<section class="home-hero">
    <div class="hero-text">
      <span class="eyebrow"><${Icon} name="sparkles" size=${14} />${greet}，歡迎回到 Video Console</span>
      <h1>今天想拍什麼？</h1>
      <p>寫下劇本或逐格新增鏡頭，用 ComfyUI 或雲端 API 生圖、生影片，再到剪輯頁排好字幕與配樂，輸出成片。</p>
      <div class="hero-actions">
        <${Button} kind="primary" icon="plus" onClick=${() => openNewProject()}>新增專案<//>
        <${Button} icon="file" onClick=${startFromScript}>從劇本開始<//>
        <${Button} icon="upload" onClick=${pickProjectZip} title="匯入「打包專案」產生的 ZIP 檔，也可以直接把 ZIP 拖進視窗">匯入專案<//>
        ${lastProject && html`<${Button} kind="ghost" icon="arrow-right" onClick=${() => openProject(lastProject.id)}
          title=${lastProject.name}>繼續「${lastProject.name}」<//>`}
      </div>
    </div>
    <div class="hero-stats">
      <${Stat} icon="clapper" label="專案" value=${projects.length} sub=${`共 ${shots} 個鏡頭`} />
      <${Stat} icon="queue" label="生成佇列" value=${running + queued}
        sub=${running ? `${running} 個執行中` : queued ? "等待執行" : "目前沒有工作"} tone=${running ? "accent" : null}
        onClick=${() => navigate("queue")} />
      <${Stat} icon="server" label="ComfyUI" tone=${tone}
        value=${enabled.length ? html`<${Dot} tone=${tone} pulse=${tone === "ok"} />${online.length} / ${enabled.length}` : "未啟用"}
        sub=${gpu ? gpu.device.name : enabled.length ? "沒有可用的伺服器" : "到引擎頁新增伺服器"}
        onClick=${() => navigate("engines", { enginesTab: "comfy" })} />
    </div>
  </section>`;
}

function ProjectCard({ p, current }) {
  const { width, height } = p.settings || {};
  const ready = (p.videos || 0) + (p.stills || 0);
  return html`<article class=${`proj-card ${current ? "is-current" : ""}`} tabindex="0" aria-label=${p.name}
      onClick=${() => openProject(p.id)} onContextMenu=${(e) => projectMenu(e, p, current ? p.id : null)}
      onKeyDown=${(e) => {
        if (e.key === "Enter") openProject(p.id);
        else if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) projectMenu(e, p, current ? p.id : null);
      }}>
    <${Thumb} pid=${p.id} path=${p.thumb || ""} ratio="16/9" fit="cover" icon="clapper" class="proj-thumb">
      <div class="proj-badges">
        ${current && html`<${Badge} tone="accent">目前開啟<//>`}
        ${p.clips > 0 && html`<${Badge} tone=${ready === p.clips ? "ok" : "neutral"}
          title="序列中已有畫面的鏡頭">${ready} / ${p.clips} 有畫面<//>`}
      </div>
      ${p.duration > 0 && html`<span class="proj-duration mono">${fmtDuration(p.duration)}</span>`}
    <//>
    <div class="proj-body">
      <div class="proj-row">
        <h3 class="proj-name" title=${p.name}>${p.name}</h3>
        <${IconButton} icon="more" size=${16} title="更多動作" class="proj-more"
          onClick=${(e) => projectMenu(e, p, current ? p.id : null)} />
      </div>
      <div class="proj-meta">
        <span>${p.shots} 個鏡頭</span>
        ${p.videos > 0 && html`<i></i><span>${p.videos} 段影片</span>`}
        ${width && html`<i></i><span class="mono">${width}×${height}</span>`}
        <span class="grow"></span>
        <span class="dim" title=${`建立於 ${fmtDate(p.created)}`}>${fmtRelTime(p.updated)}</span>
      </div>
    </div>
  </article>`;
}

function NewTile() {
  return html`<button type="button" class="proj-new" onClick=${() => openNewProject()}>
    <span class="proj-new-icon"><${Icon} name="plus" size=${22} /></span>
    <b>新增專案</b>
    <span class="dim">設定比例、尺寸與風格</span>
  </button>`;
}

function TrashList({ projects }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");
  const load = () => api.get("/api/trash").then((list) => {
    setItems([...list].sort((a, b) => b.deleted - a.deleted));
    setError("");
  }).catch((err) => setError(err.message));
  useEffect(() => {
    load();
  }, [projects]);
  const restore = async (t) => {
    try {
      await api.post(`/api/trash/${enc(t.folder)}/restore`);
      toast(`已還原「${t.name}」`, { level: "success", action: { label: "開啟", fn: () => openProject(t.id) } });
      load();
    } catch (err) {
      toastError(err, "無法還原專案");
    }
  };
  const empty = async () => {
    const ok = await confirmDialog({
      title: "清空垃圾桶？", danger: true, confirmLabel: "永久刪除",
      message: `垃圾桶裡的 ${items.length} 個專案與它們的圖片、影片會永久刪除，無法復原。`,
    });
    if (!ok) return;
    try {
      const { removed } = await api.del("/api/trash");
      toast(`已永久刪除 ${removed} 個專案`);
      load();
    } catch (err) {
      toastError(err, "無法清空垃圾桶");
    }
  };
  if (error) return html`<p class="warn-text">${error}</p>`;
  if (!items) return html`<div class="trash-loading"><${Spinner} />讀取中</div>`;
  if (!items.length) return html`<p class="dim">垃圾桶是空的。</p>`;
  return html`<div class="trash">
    ${items.map((t) => html`<div class="trash-row" key=${t.folder}>
      <${Icon} name="clapper" size=${16} />
      <span class="grow trash-name" title=${t.name}>${t.name}</span>
      <span class="dim">${t.shots} 個鏡頭</span>
      <span class="dim">刪除於 ${fmtRelTime(t.deleted)}</span>
      <${Button} size="sm" icon="undo" onClick=${() => restore(t)}>還原<//>
    </div>`)}
    <div class="trash-foot">
      <span class="dim">垃圾桶不會自動清空，佔用的空間要手動釋放。</span>
      <${Button} size="sm" kind="danger" icon="trash" onClick=${empty}>清空垃圾桶<//>
    </div>
  </div>`;
}

export function HomeView() {
  const { projects, jobs, comfy, pid, lastId } = useStore((s) => ({
    projects: s.projects, jobs: s.jobs, comfy: s.comfy, pid: s.pid,
    lastId: s.settings && s.settings.ui ? s.settings.ui.lastProjectId : null,
  }));
  const [sort, setSort] = usePref("vc.home.sort", "recent", (v) => SORT_IDS.includes(v));
  const [query, setQuery] = useState("");
  const list = useMemo(() => {
    const q = query.trim().toLowerCase();
    const hit = q ? projects.filter((p) => p.name.toLowerCase().includes(q)) : projects;
    return [...hit].sort(SORTERS[sort]);
  }, [projects, sort, query]);
  const lastProject = projects.find((p) => p.id === (pid || lastId)) || null;

  return html`<div class="view view-home">
    <div class="home-scroll">
      <${Hero} projects=${projects} jobs=${jobs} comfy=${comfy} lastProject=${lastProject} />
      ${projects.length
        ? html`<div class="home-toolbar">
            <h2>所有專案<span class="count">${projects.length}</span></h2>
            <span class="grow"></span>
            <label class="search-box">
              <${Icon} name="search" size=${15} />
              <input class="input" type="search" placeholder="搜尋專案名稱" value=${query}
                onInput=${(e) => setQuery(e.currentTarget.value)}
                onKeyDown=${(e) => { if (e.key === "Escape" && query) { e.stopPropagation(); setQuery(""); } }} />
            </label>
            <${Segmented} size="sm" value=${sort} onChange=${setSort} options=${SORTS} />
          </div>
          <div class="proj-grid">
            ${!query && html`<${NewTile} />`}
            ${list.map((p) => html`<${ProjectCard} key=${p.id} p=${p} current=${p.id === pid} />`)}
          </div>
          ${query && !list.length && html`<${Empty} icon="search" title="找不到符合的專案">換個關鍵字試試。<//>`}`
        : html`<${Empty} icon="clapper" title="還沒有任何專案" class="home-empty"
            actions=${html`<${Button} kind="primary" icon="plus" onClick=${() => openNewProject()}>新增專案<//>
              <${Button} icon="sparkles" onClick=${() => openNewProject({ starter: true })}>從範例開始<//>
              <${Button} icon="upload" onClick=${pickProjectZip}>匯入專案<//>`}>
            專案會保存鏡頭、生成結果、配樂與字幕。範例專案附 3 個示範鏡頭，可以直接試跑生圖。
          <//>`}
      <${Section} title="垃圾桶" icon="trash" collapsible id="home-trash" defaultOpen=${false} class="home-trash">
        <${TrashList} projects=${projects} />
      <//>
    </div>
  </div>`;
}
