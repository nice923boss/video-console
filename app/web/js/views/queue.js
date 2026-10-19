// Queue view: every job across ComfyUI, cloud providers and local ffmpeg, grouped into running, waiting
// and finished. Scope to the open project or all projects, filter by state, pause or clear the queue.
import { html, useState, useEffect, useMemo } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { navigate } from "../lib/actions.js";
import { pauseQueue, clearFinished, cancelAll } from "../lib/jobs-actions.js";
import { usePref } from "../lib/prefs.js";
import { KIND_LABEL } from "../lib/format.js";
import { Button, Chip, Segmented, Section, Empty, Badge } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { openMenu } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { JobRow } from "../components/job-row.js";

const FINISHED_PAGE = 120;
const PROBLEM = new Set(["failed", "canceled", "interrupted"]);
const FILTERS = [
  { id: "all", label: "全部", test: () => true },
  { id: "active", label: "進行中", test: (j) => j.status === "queued" || j.status === "running" },
  { id: "done", label: "完成", test: (j) => j.status === "done" },
  { id: "problem", label: "失敗與中斷", test: (j) => PROBLEM.has(j.status) },
];
const SCOPES = [
  { value: "project", label: "目前專案", icon: "clapper" },
  { value: "all", label: "全部專案", icon: "layers" },
];
// Same start order as queue_key in vc/jobs.py: voice-over lines first, then by queue order.
const waitOrder = (a, b) => (a.kind === "voice" ? 0 : 1) - (b.kind === "voice" ? 0 : 1)
  || (a.order ?? a.created) - (b.order ?? b.created);
const validFilter = (v) => FILTERS.some((f) => f.id === v);
const validScope = (v) => SCOPES.some((s) => s.value === v);

// The clear endpoint takes one final status at a time; failed, canceled and interrupted go together here.
async function clearProblems(pid) {
  try {
    let removed = 0;
    for (const status of PROBLEM) {
      removed += (await api.post("/api/jobs/clear", { pid, status })).removed || 0;
    }
    toast(removed ? `已清除 ${removed} 個工作紀錄` : "沒有可以清除的紀錄", { duration: 2200 });
  } catch (err) {
    toastError(err);
  }
}

// Re-render once a second while jobs are active so the elapsed and waiting times keep moving.
function useTicker(on) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!on) return undefined;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [on]);
}

function Stat({ tone, icon, label, value, onClick, active }) {
  return html`<button type="button" class=${`q-stat tone-${tone} ${active ? "active" : ""}`} onClick=${onClick}>
    <${Icon} name=${icon} size=${16} />
    <span class="q-stat-label">${label}</span>
    <b class="mono">${value}</b>
  </button>`;
}

export function QueueView() {
  const { jobs, queue, pid, project, projects } = useStore((s) => ({
    jobs: s.jobs, queue: s.queue, pid: s.pid, project: s.project, projects: s.projects,
  }));
  const [filter, setFilter] = usePref("vc.queue.filter", "all", validFilter);
  const [scopePref, setScope] = usePref("vc.queue.scope", "project", validScope);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(FINISHED_PAGE);
  const scope = pid ? scopePref : "all";
  const scopePid = scope === "project" ? pid : null;

  const names = useMemo(() => new Map((projects || []).map((p) => [p.id, p.name])), [projects]);
  const inScope = useMemo(() => jobs.filter((j) => !scopePid || j.pid === scopePid), [jobs, scopePid]);
  const counts = useMemo(() => {
    const c = { running: 0, queued: 0, done: 0, problem: 0 };
    for (const j of inScope) {
      if (j.status === "running" || j.status === "queued" || j.status === "done") c[j.status] += 1;
      else if (PROBLEM.has(j.status)) c.problem += 1;
    }
    return c;
  }, [inScope]);
  useTicker(counts.running + counts.queued > 0);

  const needle = query.trim().toLowerCase();
  const test = (FILTERS.find((f) => f.id === filter) || FILTERS[0]).test;
  const visible = inScope.filter((j) => test(j) && (!needle
    || `${j.title} ${KIND_LABEL[j.kind] || ""} ${names.get(j.pid) || ""}`.toLowerCase().includes(needle)));
  const running = visible.filter((j) => j.status === "running").sort((a, b) => (a.started || 0) - (b.started || 0));
  const waiting = visible.filter((j) => j.status === "queued").sort(waitOrder);
  const finished = visible.filter((j) => j.status !== "running" && j.status !== "queued")
    .sort((a, b) => (b.finished || b.created || 0) - (a.finished || a.created || 0));
  const active = counts.running + counts.queued;
  const ended = counts.done + counts.problem;

  const rowsOf = (list) => list.map((j) => html`<${JobRow} key=${j.id} job=${j}
    project=${project && project.id === j.pid ? project : null}
    projectName=${scope === "all" ? names.get(j.pid) || "已刪除的專案" : ""} />`);
  const clearMenu = (e) => openMenu(e, [
    { header: scopePid ? "清除這個專案的紀錄" : "清除所有專案的紀錄" },
    { label: "清除已完成", icon: "check-circle", disabled: !counts.done, onClick: () => clearFinished(scopePid, "done") },
    { label: "清除失敗與中斷", icon: "x-circle", disabled: !counts.problem,
      onClick: () => clearProblems(scopePid) },
    { label: "清除全部已結束", icon: "trash", disabled: !ended, onClick: () => clearFinished(scopePid) },
  ]);

  return html`<div class="view view-queue">
    <header class="q-head">
      <div class="q-title">
        <h2>工作佇列</h2>
        ${queue.paused ? html`<${Badge} tone="warn">已暫停<//>` : active > 0 && html`<${Badge} tone="accent">執行中<//>`}
      </div>
      <div class="q-stats">
        <${Stat} tone="accent" icon="activity" label="執行中" value=${counts.running}
          active=${filter === "active"} onClick=${() => setFilter(filter === "active" ? "all" : "active")} />
        <${Stat} tone="neutral" icon="clock" label="排隊中" value=${counts.queued}
          active=${filter === "active"} onClick=${() => setFilter(filter === "active" ? "all" : "active")} />
        <${Stat} tone="ok" icon="check-circle" label="完成" value=${counts.done}
          active=${filter === "done"} onClick=${() => setFilter(filter === "done" ? "all" : "done")} />
        <${Stat} tone="danger" icon="alert-circle" label="失敗與中斷" value=${counts.problem}
          active=${filter === "problem"} onClick=${() => setFilter(filter === "problem" ? "all" : "problem")} />
      </div>
    </header>

    <div class="q-toolbar">
      ${pid && html`<${Segmented} size="sm" value=${scope} onChange=${setScope} options=${SCOPES} />`}
      <div class="chips">
        ${FILTERS.map((f) => html`<${Chip} key=${f.id} active=${filter === f.id} onClick=${() => setFilter(f.id)}>${f.label}<//>`)}
      </div>
      <label class="search-box">
        <${Icon} name="search" size=${15} />
        <input type="search" placeholder="搜尋工作名稱或專案" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
      </label>
      <span class="grow"></span>
      <${Button} icon=${queue.paused ? "play" : "pause"} kind=${queue.paused ? "primary" : "default"}
        title=${queue.paused ? "讓排隊中的工作繼續執行" : "排隊中的工作先不開始，執行中的會跑完"}
        onClick=${() => pauseQueue(!queue.paused)}>${queue.paused ? "繼續佇列" : "暫停佇列"}<//>
      <${Button} icon="trash" disabled=${!ended} onClick=${clearMenu}>清除紀錄<//>
      <${Button} icon="x" kind="danger-ghost" disabled=${!active} onClick=${() => cancelAll(scopePid)}>全部取消<//>
    </div>

    ${queue.paused && html`<div class="note note-warn q-paused">
      <${Icon} name="pause" size=${15} />
      <span class="note-text">佇列已暫停：排隊中的 ${counts.queued} 個工作不會開始，執行中的工作會跑完。</span>
      <${Button} size="sm" kind="primary" icon="play" onClick=${() => pauseQueue(false)}>繼續<//>
    </div>`}

    <div class="q-scroll">
      ${!inScope.length
        ? html`<${Empty} icon="queue" title=${scopePid ? "這個專案還沒有工作" : "佇列是空的"}
            actions=${pid && html`<${Button} kind="primary" icon="grid" onClick=${() => navigate("board")}>到分鏡板生成畫面<//>`}>
            在分鏡板或鏡頭檢視器按「生成」後，工作會出現在這裡。ComfyUI、雲端 API 與本機 ffmpeg 各自排隊，可以同時進行。
          <//>`
        : !visible.length
          ? html`<${Empty} icon="filter" title="沒有符合條件的工作"
              actions=${html`<${Button} onClick=${() => { setFilter("all"); setQuery(""); }}>清除篩選<//>`} />`
          : html`
            ${running.length > 0 && html`<${Section} title=${`執行中（${running.length}）`} icon="activity" class="q-section">
              <div class="job-list">${rowsOf(running)}</div>
            <//>`}
            ${waiting.length > 0 && html`<${Section} title=${`排隊中（${waiting.length}）`} icon="clock" class="q-section"
                actions=${html`<span class="dim q-hint">依執行順序，配音自動排最前，其他可用「移到最前面」插隊</span>`}>
              <div class="job-list">${rowsOf(waiting)}</div>
            <//>`}
            ${finished.length > 0 && html`<${Section} title=${`已結束（${finished.length}）`} icon="history" class="q-section">
              <div class="job-list">${rowsOf(finished.slice(0, limit))}</div>
              ${finished.length > limit && html`<div class="q-more">
                <${Button} size="sm" onClick=${() => setLimit(limit + FINISHED_PAGE)}>再顯示 ${Math.min(FINISHED_PAGE, finished.length - limit)} 筆<//>
              </div>`}
            <//>`}`}
    </div>
  </div>`;
}
