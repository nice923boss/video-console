// Job submission and queue control. The server sends every job change back over SSE; the local
// upsert here only makes the new job visible before that event arrives.
import { api, upload } from "./api.js";
import { getState, setState } from "./store.js";
import { KIND_LABEL, kindOfName } from "./format.js";
import { toast, updateToast, dismissToast, toastError } from "../components/toast.js";
import { confirmDialog } from "../components/overlay.js";
import { importProjectZip, ops } from "./actions.js";

const enc = encodeURIComponent;

function upsertJobs(list) {
  setState((s) => {
    const byId = new Map(s.jobs.map((j) => [j.id, j]));
    for (const job of list) byId.set(job.id, job);
    return { jobs: [...byId.values()].sort((a, b) => (a.created || 0) - (b.created || 0)) };
  });
}

// A picture job's prompt goes into the shot's empty "生圖提示詞" box, so the shot shows the text that made it.
function keepImagePrompts(list) {
  const { pid, project } = getState();
  if (!project) return;
  const byId = new Map(project.shots.map((s) => [s.id, s]));
  const patched = new Set();
  const changes = [];
  for (const job of list) {
    const shot = job.kind === "image" && job.pid === pid && byId.get(job.shotId);
    const prompt = job.payload && job.payload.prompt;
    if (!shot || !prompt || (shot.imagePrompt || "").trim() || patched.has(shot.id)) continue;
    patched.add(shot.id);
    changes.push({ op: "shot.update", id: shot.id, patch: { imagePrompt: prompt } });
  }
  if (changes.length) ops(changes, { label: "寫入生圖提示詞", undoable: false });
}

export async function submitJob(kind, shotId, payload = {}, { quiet = false } = {}) {
  const { pid } = getState();
  if (!pid) return null;
  try {
    const { job } = await api.post("/api/jobs", { kind, pid, shotId, payload });
    upsertJobs([job]);
    keepImagePrompts([job]);
    if (!quiet) toast(`已加入佇列：${job.title}`, { duration: 2200 });
    return job;
  } catch (err) {
    toastError(err, `${KIND_LABEL[kind] || "工作"}無法送出`);
    return null;
  }
}

export async function submitBatch(list) {
  if (!list.length) return null;
  try {
    const res = await api.post("/api/jobs/batch", { jobs: list });
    upsertJobs(res.jobs);
    keepImagePrompts(res.jobs);
    const errors = res.errors || [];
    if (res.jobs.length) toast(`已送出 ${res.jobs.length} 個工作`, { level: "success", duration: 2400 });
    if (errors.length) {
      const lines = [...new Set(errors.map((e) => e.error))].slice(0, 3).join("\n");
      toast(lines, { level: "error", title: `${errors.length} 個工作無法送出` });
    }
    return res;
  } catch (err) {
    toastError(err, "工作無法送出");
    return null;
  }
}

// kind: image | video | upscale | upscalevideo | kenburns | lastframe. Extra copies of the same shot get a seed
// offset, which only matters when the shot's seed is locked (otherwise every job rolls its own).
export function generate(kind, shotIds, { count = 1, payload = {} } = {}) {
  const { pid } = getState();
  const list = [];
  for (const shotId of shotIds) {
    for (let i = 0; i < count; i += 1) {
      list.push({ kind, pid, shotId, payload: count > 1 ? { ...payload, seedOffset: i } : payload });
    }
  }
  if (list.length === 1) return submitJob(kind, shotIds[0], list[0].payload);
  return submitBatch(list);
}

async function jobCall(jid, verb, failTitle) {
  try {
    const job = await api.post(`/api/jobs/${enc(jid)}/${verb}`);
    if (job && job.id) upsertJobs([job]);
    return job;
  } catch (err) {
    toastError(err, failTitle);
    return null;
  }
}

export const cancelJob = (jid) => jobCall(jid, "cancel", "無法取消工作");
export const retryJob = (jid) => jobCall(jid, "retry", "無法重試工作");
export const moveJobTop = (jid) => jobCall(jid, "top", "無法調整順序");

export async function removeJob(jid) {
  try {
    await api.del(`/api/jobs/${enc(jid)}`);
    setState((s) => ({ jobs: s.jobs.filter((j) => j.id !== jid) }));
  } catch (err) {
    toastError(err, "無法移除工作");
  }
}

export async function pauseQueue(paused) {
  try {
    const state = await api.post("/api/jobs/pause", { paused });
    if (state) setState({ queue: state });
    toast(paused ? "佇列已暫停，執行中的工作會跑完" : "佇列已繼續", { duration: 2200 });
  } catch (err) {
    toastError(err);
  }
}

export async function clearFinished(pid = null, status = null) {
  try {
    const { removed } = await api.post("/api/jobs/clear", { pid, status });
    toast(removed ? `已清除 ${removed} 個工作紀錄` : "沒有可以清除的紀錄", { duration: 2200 });
  } catch (err) {
    toastError(err);
  }
}

export async function cancelAll(pid = null) {
  const ok = await confirmDialog({
    title: pid ? "取消這個專案的所有工作？" : "取消所有工作？",
    message: "排隊中的工作會直接取消，執行中的工作會嘗試中斷。",
    confirmLabel: "全部取消",
    danger: true,
  });
  if (!ok) return;
  try {
    const { canceled } = await api.post("/api/jobs/cancel-all", { pid });
    toast(`已取消 ${canceled} 個工作`, { duration: 2400 });
  } catch (err) {
    toastError(err);
  }
}

// Upload files one after another; each becomes an ingest job on the server.
// target: "library" (media library) or "shot" (a new take on `shot`). track: also add audio to the
// soundtrack at `start` seconds, on `audioTrack` ("voice" or a music track) when given and free there.
export async function uploadFiles(files, { target = "library", shot = null, track = false, start = null, layer = null, audioTrack = null } = {}) {
  const { pid } = getState();
  if (!pid) return [];
  const list = [...files].filter((f) => {
    if (kindOfName(f.name)) return true;
    if (/\.zip$/i.test(f.name)) importProjectZip(f);
    else toast(`不支援的檔案類型：${f.name}`, { level: "warn" });
    return false;
  });
  if (!list.length) return [];
  const id = toast(`正在上傳 ${list.length} 個檔案…`, { duration: 0, progress: 0 });
  const jobs = [];
  for (let i = 0; i < list.length; i += 1) {
    const file = list[i];
    const audio = kindOfName(file.name) === "audio";
    const params = new URLSearchParams({ name: file.name, target: audio ? "library" : target });
    if (shot && !audio) params.set("shot", shot);
    if (layer && !audio) params.set("layer", layer);
    if (track || audio) params.set("track", track ? "1" : "0");
    if (track && audio && audioTrack) params.set("audioTrack", audioTrack);
    if (start != null) params.set("start", String(start));
    try {
      const res = await upload(`/api/projects/${enc(pid)}/upload?${params}`, file, (p) => {
        updateToast(id, { message: `正在上傳 ${file.name}（${i + 1}/${list.length}）`, progress: (i + p) / list.length });
      });
      if (res && res.job) jobs.push(res.job);
    } catch (err) {
      toastError(err, `${file.name} 上傳失敗`);
    }
  }
  if (jobs.length) {
    upsertJobs(jobs);
    updateToast(id, { message: `已上傳 ${jobs.length} 個檔案，正在處理縮圖與格式`, level: "success", progress: null, duration: 3000 });
  } else {
    dismissToast(id);
  }
  return jobs;
}

export function activeJobsFor(jobs, pid, shotId) {
  return jobs.filter((j) => j.pid === pid && j.shotId === shotId && (j.status === "queued" || j.status === "running"));
}
