// Several pictures of one shot at once (IM-08): once every copy has finished, a toast offers to lay them out
// side by side, and a click makes one the shot's picture. The other copies stay in the "版本" tab.
import { html } from "../lib/h.js";
import { getState, subscribe } from "../lib/store.js";
import { updateShot } from "../lib/actions.js";
import { shotLabel } from "../lib/format.js";
import { Thumb } from "../components/media.js";
import { openModal, Modal } from "../components/overlay.js";
import { toast } from "../components/toast.js";

const FINISHED = ["done", "failed", "canceled", "interrupted"];

function PickTake({ pid, project, shot, takes, close }) {
  const ratio = `${project.settings.width}/${project.settings.height}`;
  const pick = (t) => {
    updateShot(shot.id, { activeImage: t.id }, { label: "挑選使用中的圖片" });
    close(t.id);
  };
  return html`<${Modal} title=${`挑一張給${shotLabel(shot, project.shots.indexOf(shot))}`} wide onClose=${() => close(null)}>
    <p class="modal-text">點一張設為鏡頭圖片；沒選到的仍留在「版本」頁。</p>
    <div class="pick-take-grid">
      ${takes.map((t, i) => html`<button type="button" key=${t.id}
          class=${`ref-cell ${shot.activeImage === t.id ? "active" : ""}`} onClick=${() => pick(t)}>
        <${Thumb} pid=${pid} path=${t.thumb || t.file} ratio=${ratio} fit="contain" />
        <span class="ref-cell-label">第 ${i + 1} 張</span>
      </button>`)}
    </div>
  <//>`;
}

export function openPickTake(pid, shotId, takeIds) {
  const { project } = getState();
  const shot = project && project.id === pid && project.shots.find((s) => s.id === shotId);
  const takes = shot ? takeIds.map((id) => project.takes[id]).filter(Boolean) : [];
  if (!takes.length) return toast("這些圖片或鏡頭已經刪除了", { level: "error" });
  return openModal((close) => html`<${PickTake} pid=${pid} project=${project} shot=${shot} takes=${takes} close=${close} />`);
}

// Waits for the copies of one shot; offers the picker when at least two came back as pictures.
export function watchPick(pid, shotId, jobIds) {
  if (jobIds.length < 2) return;
  const stop = subscribe((s) => {
    const jobs = jobIds.map((id) => s.jobs.find((j) => j.id === id));
    if (jobs.some((j) => j && !FINISHED.includes(j.status))) return;
    stop();
    const takeIds = jobs.filter((j) => j && j.status === "done").flatMap((j) => (j.result && j.result.takeIds) || []);
    if (takeIds.length < 2 || s.pid !== pid) return;
    toast(`${takeIds.length} 張圖片都生好了`, { level: "success", duration: 20000,
      action: { label: "挑一張", fn: () => openPickTake(pid, shotId, takeIds) } });
  });
}
