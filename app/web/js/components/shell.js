// App frame: the navigation rail on the left, the top bar and the status bar.
import { html, useMemo } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { navigate, openProject, undo, redo, updateProject, saveSettings, revealInFolder, duplicateProject } from "../lib/actions.js";
import { fmtTime, fmtBytes } from "../lib/format.js";
import { buildSequence, sequenceDuration } from "../lib/sequence.js";
import { useTheme } from "../lib/theme.js";
import { keyHint, keysOf, selectKeymap } from "../lib/keymap.js";
import { setWatch } from "../lib/watch.js";
import { Icon } from "./icons.js";
import { Dot, Keys, Spinner, Button, IconButton } from "./ui.js";
import { openMenu, promptDialog } from "./overlay.js";
import { openRevisions } from "../dialogs/revisions-dialog.js";

export const VIEWS = [
  { id: "home", label: "專案", icon: "home" },
  { id: "board", label: "分鏡", icon: "grid", project: true },
  { id: "edit", label: "剪輯", icon: "scissors", project: true },
  { id: "library", label: "素材", icon: "images", project: true },
  { id: "queue", label: "佇列", icon: "queue" },
  { id: "engines", label: "引擎", icon: "cpu" },
  { id: "settings", label: "設定", icon: "settings" },
  { id: "help", label: "說明", icon: "book" },
];

export const VIEW_LABEL = Object.fromEntries(VIEWS.map((v) => [v.id, v.label]));

// ok: every enabled ComfyUI server answers; warn: some do; danger: none; neutral: none enabled.
export function comfyTone(comfy) {
  const enabled = (comfy || []).filter((c) => c.enabled !== false);
  if (!enabled.length) return "neutral";
  const online = enabled.filter((c) => c.online).length;
  if (online === enabled.length) return "ok";
  return online ? "warn" : "danger";
}

function jobCounts(jobs) {
  let running = 0;
  let queued = 0;
  let progress = null;
  for (const j of jobs) {
    if (j.status === "running") {
      running += 1;
      if (progress == null) progress = j.progress || 0;
    } else if (j.status === "queued") {
      queued += 1;
    }
  }
  return { running, queued, progress };
}

export function Rail({ commands }) {
  const { view, pid, jobs, comfy, theme } = useStore((s) => ({
    view: s.view, pid: s.pid, jobs: s.jobs, comfy: s.comfy, theme: s.settings && s.settings.ui && s.settings.ui.theme,
  }));
  useStore(selectKeymap); // re-render the key hints after a rebind
  const { running, queued } = useMemo(() => jobCounts(jobs), [jobs]);
  const tone = comfyTone(comfy);
  const dark = useTheme() === "dark";
  const flipTheme = () => saveSettings({ ui: { theme: dark ? "light" : "dark" } });

  return html`<nav class="rail" aria-label="主要導覽">
    <button type="button" class="rail-logo" title="Video Console" onClick=${() => navigate("home")}>
      <span>VC</span>
    </button>
    <div class="rail-items">
      ${VIEWS.map((v) => {
        const active = view === v.id;
        const dim = v.project && !pid;
        const badge = v.id === "queue" && running + queued ? running + queued : null;
        return html`<button type="button" key=${v.id} class=${`rail-item ${active ? "active" : ""} ${dim ? "dim" : ""}`}
            title=${`${v.label}${keyHint(`view:${v.id}`)}`} aria-current=${active ? "page" : undefined}
            onClick=${() => navigate(v.id)}>
          <span class="rail-icon">
            <${Icon} name=${v.icon} size=${20} />
            ${badge != null && html`<span class=${`rail-badge ${running ? "busy" : ""}`}>${badge > 99 ? "99+" : badge}</span>`}
            ${v.id === "engines" && html`<span class=${`rail-dot dot-${tone}`}></span>`}
          </span>
          <span class="rail-label">${v.label}</span>
        </button>`;
      })}
    </div>
    <div class="rail-foot">
      <button type="button" class="rail-mini" title=${`指令面板${keyHint("app.palette")}`} aria-label="指令面板"
        onClick=${commands.palette}><${Icon} name="command" size=${18} /></button>
      <button type="button" class="rail-mini" title=${`快捷鍵一覽${keyHint("help:keys")}`} aria-label="快捷鍵一覽"
        onClick=${commands.shortcuts}><${Icon} name="keyboard" size=${18} /></button>
      <button type="button" class="rail-mini" title=${theme === "system" ? "主題：跟隨系統（點一下切換）" : dark ? "切換成淺色主題" : "切換成深色主題"}
        aria-label="切換主題" onClick=${flipTheme}><${Icon} name=${dark ? "sun" : "moon"} size=${18} /></button>
    </div>
  </nav>`;
}

function projectMenu(e, { project, projects, commands }) {
  const recent = [...projects].sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, 8);
  const rename = async () => {
    const name = await promptDialog({ title: "重新命名專案", label: "專案名稱", value: project.name, confirmLabel: "儲存" });
    if (name && name !== project.name) updateProject({ name }, { label: "重新命名專案" });
  };
  openMenu(e, [
    recent.length && { header: "最近的專案" },
    ...recent.map((p) => ({
      label: p.name, checked: project ? p.id === project.id : false,
      hint: `${p.shots} 鏡`, onClick: () => (project && p.id === project.id ? null : openProject(p.id)),
    })),
    recent.length && { divider: true },
    project && { label: "重新命名專案…", icon: "pencil", onClick: rename },
    project && { label: "建立副本…", icon: "copy", onClick: () => duplicateProject(project.id) },
    project && { label: "開啟專案資料夾", icon: "folder-open", onClick: () => revealInFolder() },
    { label: "所有專案", icon: "home", onClick: () => navigate("home") },
    { label: "新增專案…", icon: "plus", onClick: commands.newProject },
  ]);
}

function QueueChip() {
  const { jobs, paused } = useStore((s) => ({ jobs: s.jobs, paused: s.queue.paused }));
  const { running, queued, progress } = useMemo(() => jobCounts(jobs), [jobs]);
  let body;
  let tone = "idle";
  if (running) {
    tone = "busy";
    body = html`<${Spinner} size=${14} /><span>${running} 執行中${queued ? `，${queued} 排隊` : ""}</span>
      ${progress != null && html`<span class="qchip-bar"><i style=${`width:${Math.round(progress * 100)}%`}></i></span>`}`;
  } else if (queued) {
    tone = paused ? "paused" : "busy";
    body = html`<${Icon} name=${paused ? "pause" : "clock"} size=${14} /><span>${queued} 排隊中</span>`;
  } else {
    body = html`<${Icon} name="check" size=${14} /><span>佇列閒置</span>`;
  }
  if (paused) tone = "paused";
  return html`<button type="button" class=${`qchip qchip-${tone}`} title=${paused ? "佇列已暫停" : "開啟工作佇列"}
      onClick=${() => navigate("queue")}>
    ${paused && !queued && !running ? html`<${Icon} name="pause" size=${14} /><span>佇列已暫停</span>` : body}
  </button>`;
}

export function TopBar({ commands }) {
  const { project, projects, view, u, watch } = useStore((s) => ({
    project: s.project, projects: s.projects, view: s.view, u: s.undo, watch: s.watch,
  }));
  useStore(selectKeymap); // re-render the key hints after a rebind
  const stats = useMemo(() => {
    if (!project) return null;
    const clips = buildSequence(project);
    return { shots: project.shots.length, duration: sequenceDuration(clips) };
  }, [project]);

  return html`<header class="topbar">
    <div class="tb-left">
      <button type="button" class="tb-project" title="切換專案"
          onClick=${(e) => projectMenu(e, { project, projects, commands })}>
        <span class="tb-project-name">${project ? project.name : "Video Console"}</span>
        <${Icon} name="chevron-down" size=${15} />
      </button>
      ${stats && html`<span class="tb-meta">
        <span>${stats.shots} 個鏡頭</span><i></i><span class="mono">${fmtTime(stats.duration, { tenths: false })}</span>
        <i></i><span>${project.settings.width}×${project.settings.height}</span>
      </span>`}
    </div>
    <button type="button" class="tb-search" onClick=${commands.palette}>
      <${Icon} name="search" size=${15} />
      <span>搜尋指令、鏡頭或專案</span>
      ${keysOf("app.palette")[0] && html`<${Keys} combo=${keysOf("app.palette")[0]} />`}
    </button>
    <div class="tb-right">
      ${project && watch && html`<button type="button" class="tb-watch" onClick=${() => setWatch(false)}
          title="這個視窗只顯示專案的即時變化，不會修改它；按一下結束觀看">
        <${Icon} name="eye" size=${15} /><span>觀看模式</span><${Icon} name="x" size=${13} />
      </button>`}
      ${project && !watch && html`<div class="tb-history">
        <${IconButton} icon="undo" disabled=${!u.undo} onClick=${undo}
          title=${u.undo ? `復原：${u.undoLabel || "上一步"}${keyHint("app.undo")}` : "沒有可以復原的步驟"} />
        <${IconButton} icon="redo" disabled=${!u.redo} onClick=${redo}
          title=${u.redo ? `重做：${u.redoLabel || "下一步"}${keyHint("app.redo")}` : "沒有可以重做的步驟"} />
      </div>`}
      <${QueueChip} />
      ${project && view !== "home" && !watch && html`<${Button} icon="message" onClick=${openRevisions}
        title="載入客戶從審稿頁回傳的 ZIP，逐條審核、套用">修改意見<//>`}
      ${project && view !== "home" && !watch && html`<${Button} kind="primary" icon="film" onClick=${commands.export}>輸出影片<//>`}
    </div>
  </header>`;
}

function ComfyStatus({ server }) {
  const d = server.device;
  const pending = server.queue ? (server.queue.running || 0) + (server.queue.pending || 0) : 0;
  const tone = !server.online ? "danger" : pending ? "accent" : "ok";
  const title = server.online
    ? `${server.name}：已連線${server.latencyMs != null ? `，延遲 ${server.latencyMs} ms` : ""}${d ? `\n${d.name}` : ""}`
    : `${server.name}：${server.error || "離線"}`;
  return html`<button type="button" class="sb-item sb-btn" title=${title}
      onClick=${() => navigate("engines", { enginesTab: "comfy" })}>
    <${Dot} tone=${tone} pulse=${!!pending} />
    <span>${server.name}</span>
    ${server.online && d && d.vramTotal > 0 && html`<span class="sb-dim mono">
      VRAM ${fmtBytes(d.vramTotal - d.vramFree)} / ${fmtBytes(d.vramTotal)}</span>`}
    ${server.online && pending > 0 && html`<span class="sb-dim">佇列 ${pending}</span>`}
    ${!server.online && html`<span class="sb-dim">離線</span>`}
  </button>`;
}

export function StatusBar() {
  const { connected, comfy, rev, paused, jobs, version } = useStore((s) => ({
    connected: s.connected, comfy: s.comfy, rev: s.project ? s.project.rev : null, paused: s.queue.paused,
    jobs: s.jobs, version: s.version,
  }));
  const { running, queued } = useMemo(() => jobCounts(jobs), [jobs]);
  const servers = (comfy || []).filter((c) => c.enabled !== false);
  return html`<footer class="statusbar">
    <span class=${`sb-item ${connected ? "" : "sb-bad"}`} title=${connected ? "與本機伺服器的連線正常" : "連線中斷，正在自動重試"}>
      <${Dot} tone=${connected ? "ok" : "danger"} pulse=${!connected} />
      <span>${connected ? "已連線" : "連線中斷，重試中"}</span>
    </span>
    ${servers.map((s) => html`<${ComfyStatus} key=${s.id} server=${s} />`)}
    <span class="sb-item">
      <${Icon} name="queue" size=${13} />
      <span>${paused ? "佇列已暫停" : running || queued ? `${running} 執行中，${queued} 排隊` : "沒有工作"}</span>
    </span>
    <span class="sb-spacer"></span>
    ${rev != null && html`<span class="sb-item" title="每次修改都會立即存檔">
      <${Icon} name="check" size=${13} /><span>已儲存（第 ${rev} 版）</span>
    </span>`}
    ${version && html`<span class="sb-item sb-dim mono">v${version}</span>`}
    <span class="sb-credit">製作者：黃政文</span>
  </footer>`;
}
