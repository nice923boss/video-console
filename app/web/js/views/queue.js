// Queue view: every job across ComfyUI, cloud providers and local ffmpeg, grouped into running, waiting
// and finished. Scope to the open project or all projects, filter by state, pause or clear the queue.
import { html, useState, useMemo } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { navigate } from "../lib/actions.js";
import { pauseQueue, clearFinished, cancelAll, moveJob, scheduleQueue } from "../lib/jobs-actions.js";
import { usePref } from "../lib/prefs.js";
import { KIND_LABEL, STATUS_LABEL, fmtDate, fmtDateTime } from "../lib/format.js";
import { Button, Chip, Segmented, Section, Empty, Badge, Toggle } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { Modal, openMenu, openModal } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { JobRow, laneLabel } from "../components/job-row.js";

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

// Job log as CSV (time taken, where it ran, which template or provider), oldest first. The byte-order mark
// lets Excel read the Chinese text as UTF-8.
const CSV_HEAD = ["建立時間", "結束時間", "專案", "類型", "工作", "狀態", "執行位置／模型", "用時（秒）", "嘗試次數", "自動重試", "錯誤"];
const csvCell = (v) => (/[",\r\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));

function exportCsv(list, names, ctx) {
  const rows = [...list].sort((a, b) => (a.created || 0) - (b.created || 0)).map((j) => [
    fmtDateTime(j.created), fmtDateTime(j.finished), names.get(j.pid) || "", KIND_LABEL[j.kind] || j.kind, j.title,
    STATUS_LABEL[j.status] || j.status, laneLabel(j, ctx), j.seconds ?? "", j.attempt || 1, j.autoRetries || 0,
    j.error || "",
  ]);
  const text = [CSV_HEAD, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob(["\ufeff" + text], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `工作記錄-${fmtDateTime(Date.now() / 1000).replace(/[/: ]/g, "")}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Waiting jobs can be dragged up or down (QU-02): dropped on a row's upper half the job goes before that
// row, on the lower half after it. Voice-over lines still start first whatever their place.
function WaitingList({ list, rowOf }) {
  const [drag, setDrag] = useState(null);
  const [mark, setMark] = useState(null);
  const lowerHalf = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientY > r.top + r.height / 2;
  };
  const over = (e, job) => {
    if (!drag || drag === job.id) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const after = lowerHalf(e);
    if (mark?.id !== job.id || mark.after !== after) setMark({ id: job.id, after });
  };
  const drop = (e, i) => {
    e.preventDefault();
    const before = lowerHalf(e) ? list[i + 1]?.id ?? null : list[i].id;
    if (drag && before !== drag) moveJob(drag, before);
    setDrag(null);
    setMark(null);
  };
  return html`<div class="job-list">${list.map((j, i) => html`<div key=${j.id} draggable="true" title="拖曳可調整順序"
    class=${`q-drag ${drag === j.id ? "dragging" : ""} ${mark?.id === j.id ? (mark.after ? "drop-after" : "drop-before") : ""}`}
    onDragStart=${(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", j.title); setDrag(j.id); }}
    onDragEnd=${() => { setDrag(null); setMark(null); }}
    onDragOver=${(e) => over(e, j)} onDrop=${(e) => drop(e, i)}>${rowOf(j)}</div>`)}</div>`;
}

// Night run (QU-06): the first moment after `from` (epoch seconds) that the clock shows hh:mm.
function nextAt(hhmm, from) {
  const [h, m] = hhmm.split(":").map(Number);
  const d = new Date(from * 1000);
  d.setHours(h, m, 0, 0);
  if (d.getTime() / 1000 <= from) d.setDate(d.getDate() + 1);
  return d.getTime() / 1000;
}

function whenText(ts) {
  const midnight = (t) => new Date(t).setHours(0, 0, 0, 0);
  const days = Math.round((midnight(ts * 1000) - midnight(Date.now())) / 86400000);
  return `${["今天", "明天", "後天"][days] ?? fmtDate(ts)} ${fmtDateTime(ts).slice(-5)}`;
}

function ScheduleDialog({ close }) {
  const [start, setStart] = useState("23:00");
  const [useStop, setUseStop] = useState(false);
  const [stop, setStop] = useState("07:00");
  const startAt = start ? nextAt(start, Date.now() / 1000) : null;
  const stopAt = useStop && stop && startAt ? nextAt(stop, startAt) : null;
  return html`<${Modal} title="排程佇列" width=${440} onClose=${() => close(null)}
      footer=${html`<button type="button" class="btn" onClick=${() => close(null)}>取消</button>
        <button type="button" class="btn btn-primary" disabled=${!startAt}
          onClick=${() => close({ start: startAt, stop: stopAt })}>排程</button>`}>
    <p class="modal-text">佇列先暫停，到開始時間才執行排隊中的工作。設了暫停時間，到時就不再開始新工作，執行中的會跑完。只排這一次。</p>
    <label class="field">
      <span class="field-label">開始時間</span>
      <input class="input" type="time" value=${start} data-autofocus onInput=${(e) => setStart(e.currentTarget.value)} />
    </label>
    <${Toggle} checked=${useStop} label="到點自動暫停" onChange=${setUseStop} />
    ${useStop && html`<label class="field">
      <span class="field-label">暫停時間</span>
      <input class="input" type="time" value=${stop} onInput=${(e) => setStop(e.currentTarget.value)} />
    </label>`}
    ${startAt && html`<p class="field-hint">${whenText(startAt)} 開始${stopAt ? `，${whenText(stopAt)} 暫停` : ""}</p>`}
  <//>`;
}

async function openSchedule() {
  const v = await openModal((close) => html`<${ScheduleDialog} close=${close} />`);
  if (v && (await scheduleQueue(v.start, v.stop))) toast(`佇列會在 ${whenText(v.start)} 開始`, { duration: 2600 });
}

function Stat({ tone, icon, label, value, onClick, active }) {
  return html`<button type="button" class=${`q-stat tone-${tone} ${active ? "active" : ""}`} onClick=${onClick}>
    <${Icon} name=${icon} size=${16} />
    <span class="q-stat-label">${label}</span>
    <b class="mono">${value}</b>
  </button>`;
}

export function QueueView() {
  const { jobs, queue, pid, project, projects, providers, workflows, comfy } = useStore((s) => ({
    jobs: s.jobs, queue: s.queue, pid: s.pid, project: s.project, projects: s.projects,
    providers: s.providers, workflows: s.workflows, comfy: s.comfy,
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

  const rowOf = (j) => html`<${JobRow} key=${j.id} job=${j}
    project=${project && project.id === j.pid ? project : null}
    projectName=${scope === "all" ? names.get(j.pid) || "已刪除的專案" : ""} />`;
  const rowsOf = (list) => list.map(rowOf);
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
        ${queue.resumeAt ? html`<${Badge} tone="info">已排程<//>`
          : queue.paused ? html`<${Badge} tone="warn">已暫停<//>` : active > 0 && html`<${Badge} tone="accent">執行中<//>`}
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
      <${Button} icon="moon" title="設定時間才開始跑佇列，例如晚上不用電腦時" onClick=${openSchedule}>排程<//>
      <${Button} icon="download" disabled=${!visible.length} title="把目前列出的工作存成 CSV（用時、執行位置、錯誤），可用 Excel 開啟"
        onClick=${() => exportCsv(visible, names, { providers, workflows, comfy })}>匯出 CSV<//>
      <${Button} icon="trash" disabled=${!ended} onClick=${clearMenu}>清除紀錄<//>
      <${Button} icon="x" kind="danger-ghost" disabled=${!active} onClick=${() => cancelAll(scopePid)}>全部取消<//>
    </div>

    ${queue.resumeAt ? html`<div class="note note-info q-paused">
      <${Icon} name="moon" size=${15} />
      <span class="note-text">已排程：${whenText(queue.resumeAt)} 自動開始${queue.pauseAt ? `，${whenText(queue.pauseAt)} 自動暫停` : ""}。在那之前排隊中的 ${counts.queued} 個工作不會開始。</span>
      <${Button} size="sm" onClick=${() => scheduleQueue(null, null)}>取消排程<//>
      <${Button} size="sm" kind="primary" icon="play" onClick=${() => pauseQueue(false)}>立即開始<//>
    </div>` : queue.paused ? html`<div class="note note-warn q-paused">
      <${Icon} name="pause" size=${15} />
      <span class="note-text">佇列已暫停：排隊中的 ${counts.queued} 個工作不會開始，執行中的工作會跑完。</span>
      <${Button} size="sm" kind="primary" icon="play" onClick=${() => pauseQueue(false)}>繼續<//>
    </div>` : queue.pauseAt && html`<div class="note note-info q-paused">
      <${Icon} name="moon" size=${15} />
      <span class="note-text">佇列會在 ${whenText(queue.pauseAt)} 自動暫停，執行中的工作會跑完。</span>
      <${Button} size="sm" onClick=${() => scheduleQueue(null, null)}>取消<//>
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
                actions=${html`<span class="dim q-hint">依執行順序，配音自動排最前；其他工作可拖曳調整，或用「移到最前面」插隊</span>`}>
              <${WaitingList} list=${waiting} rowOf=${rowOf} />
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
