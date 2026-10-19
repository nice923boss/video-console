// Project version history: snapshots the server keeps while you work (Settings > 版本紀錄). Restoring
// one is itself an undoable edit, so a wrong pick is one Ctrl+Z away.
import { html, useState, useEffect } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { loadProject, undo } from "../lib/actions.js";
import { fmtDateTime, fmtRelTime } from "../lib/format.js";
import { openModal, Modal, confirmDialog } from "../components/overlay.js";
import { Button, Empty, Spinner } from "../components/ui.js";
import { toast, toastError } from "../components/toast.js";

const enc = encodeURIComponent;

function HistoryBody({ pid, close }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(null);
  const project = getState().project;
  useEffect(() => {
    api.get(`/api/projects/${enc(pid)}/history`).then(setList, (err) => setError(err.message));
  }, [pid]);

  const restore = async (item) => {
    const ok = await confirmDialog({
      title: `還原到 r${item.rev}？`,
      message: `專案內容會換成 ${fmtDateTime(item.time)} 的版本（${item.shots} 個鏡頭）。`
        + "還原本身也會記進復原紀錄，按 Ctrl+Z 就能回到現在的內容。",
      confirmLabel: "還原這個版本",
    });
    if (!ok) return;
    setBusy(item.rev);
    try {
      await api.post(`/api/projects/${enc(pid)}/history/${item.rev}/restore`);
      await loadProject(pid);
      toast(`已還原到 r${item.rev}`, { level: "success", action: { label: "復原", fn: undo } });
      close(item.rev);
    } catch (err) {
      toastError(err, "無法還原版本");
      setBusy(null);
    }
  };

  const footer = html`<span class="modal-foot-note dim">間隔與保留份數在「設定 › 版本紀錄」調整</span>
    <${Button} onClick=${() => close(null)}>關閉<//>`;
  let body;
  if (error) {
    body = html`<div class="note note-danger"><span class="note-text"><b>讀不到版本紀錄</b>：${error}</span></div>`;
  } else if (!list) {
    body = html`<div class="hist-loading"><${Spinner} /><span class="dim">讀取中…</span></div>`;
  } else if (!list.length) {
    body = html`<${Empty} icon="history" title="還沒有版本紀錄">
      修改專案一段時間後，伺服器會自動存下修改前的版本。<//>`;
  } else {
    body = html`<ol class="hist-list">
      ${project && html`<li class="hist-row current">
        <span class="hist-rev">r${project.rev}</span>
        <span class="hist-when"><b>目前的內容</b><span class="dim">${fmtRelTime(project.updated)}</span></span>
        <span class="hist-meta">${(project.shots || []).length} 個鏡頭</span>
        <span class="hist-act"></span>
      </li>`}
      ${list.map((item) => html`<li class="hist-row" key=${item.rev}>
        <span class="hist-rev">r${item.rev}</span>
        <span class="hist-when"><b>${fmtDateTime(item.time)}</b><span class="dim">${fmtRelTime(item.time)}</span></span>
        <span class="hist-meta">${item.shots} 個鏡頭，${item.takes} 個版本素材</span>
        <span class="hist-act">
          <${Button} size="sm" icon="history" busy=${busy === item.rev} disabled=${busy != null}
            onClick=${() => restore(item)}>還原<//>
        </span>
      </li>`)}
    </ol>`;
  }
  return html`<${Modal} title=${`版本紀錄：${(project && project.name) || ""}`} width=${640} footer=${footer}
      onClose=${() => close(null)} class="history-dialog">
    ${body}
  <//>`;
}

export function openHistory() {
  const { pid } = getState();
  if (!pid) return Promise.resolve(null);
  return openModal((close) => html`<${HistoryBody} pid=${pid} close=${close} />`);
}
