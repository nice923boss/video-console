// Project, shot and settings actions shared by the views. Every edit goes through `ops`: the server
// applies it, records it for undo and broadcasts the new document back as a "project" event.
import { api, upload } from "./api.js";
import { getState, setState } from "./store.js";
import { applyTheme } from "./theme.js";
import { newId, shotLabel, fmtBytes } from "./format.js";
import { toast, updateToast, dismissToast, toastError } from "../components/toast.js";
import { confirmDialog } from "../components/overlay.js";
import { readPref } from "./prefs.js";
import { watchRefuses } from "./watch.js";
import { openDefaultUpdates } from "../dialogs/default-updates.js";
import { maybeOpenTour } from "../dialogs/tour.js";
import { announceRelease } from "./release.js";
import { undoKeyText } from "./keymap.js";

const EMPTY_UNDO = { undo: 0, redo: 0, undoLabel: "", redoLabel: "" };
// With linking on (the timeline's link toggle), shot edits carry the items on the other tracks along.
export const LINK_PREF = "vc.timeline.link";
const SHOT_OPS = new Set(["shot.add", "shot.delete", "shot.move", "shot.update"]);
const linking = (list) => readPref(LINK_PREF, true, (v) => typeof v === "boolean") && list.some((o) => SHOT_OPS.has(o.op));
// A shot taken out of the sequence holds the items that lay on it (vc/linked.py). With linking off, shot
// edits still go as "linked" without the shift while any shot holds some, so one put back gets them again.
const holding = (list) => list.some((o) => SHOT_OPS.has(o.op)) && ((getState().project || {}).shots || []).some((x) => x.parked);
const enc = encodeURIComponent;
let bootedOnce = false;

// Drop selection entries that no longer exist in `project`.
export function pruneSelection(s, project) {
  const ids = new Set(project.shots.map((x) => x.id));
  const selected = s.selected.filter((id) => ids.has(id));
  const focus = s.focus && ids.has(s.focus) ? s.focus : null;
  let editSel = s.editSel;
  if (editSel) {
    const list = { shot: project.shots, audio: project.audio, overlay: project.overlays, adjust: project.adjustments }[editSel.type] || project.captions;
    if (!(list || []).some((x) => x.id === editSel.id)) editSel = null;
  }
  return { selected: selected.length === s.selected.length ? s.selected : selected, focus, editSel };
}

export async function bootstrap() {
  let data;
  try {
    data = await api.get("/api/bootstrap");
  } catch (err) {
    if (getState().boot !== "ready") setState({ boot: "error", bootError: err.message });
    return;
  }
  applyTheme(data.settings);
  // "ready" waits until the last project is open, so the first screen is already the right one.
  setState({
    instance: data.instance, version: data.version, settings: data.settings,
    secrets: data.secrets || {}, projects: data.projects || [], jobs: data.jobs || [], queue: data.queue,
    comfy: data.comfy || [], workflows: data.workflows || [], providers: data.providers || [],
    presets: data.presets || {}, system: data.system || {}, costs: data.costs || null,
  });
  const first = !bootedOnce;
  bootedOnce = true;
  const s = getState();
  let pid = s.pid;
  if (!pid && first) pid = (data.settings.ui || {}).lastProjectId || null;
  if (pid && s.projects.some((p) => p.id === pid)) {
    try {
      await loadProject(pid);
      if (first && !location.hash.slice(1)) setState({ view: "board" });
    } catch (err) {
      toastError(err, "無法開啟上次的專案");
      closeProject();
    }
  } else if (s.pid) {
    closeProject();
    toast("目前的專案已不存在，已回到專案列表", { level: "warn" });
  }
  setState({ boot: "ready", bootError: null });
  if (first && data.defaultUpdates && data.defaultUpdates.length) openDefaultUpdates(data.defaultUpdates);
  else if (first) maybeOpenTour(s.projects.length > 0);
  if (first) announceRelease();
}

export async function loadProject(pid) {
  const data = await api.get(`/api/projects/${enc(pid)}`);
  setState((s) => {
    if (s.pid === pid) {
      if (s.project && s.project.rev > data.project.rev) return null;
      return { project: data.project, undo: data.undo || EMPTY_UNDO, ...pruneSelection(s, data.project) };
    }
    const first = data.project.shots[0];
    return {
      pid, project: data.project, undo: data.undo || EMPTY_UNDO, editSel: null, editRange: null,
      selected: first ? [first.id] : [], focus: first ? first.id : null,
    };
  });
}

export async function openProject(pid, view = "board") {
  try {
    await api.post(`/api/projects/${enc(pid)}/open`);
    await loadProject(pid);
    setState({ view });
    return true;
  } catch (err) {
    toastError(err, "無法開啟專案");
    return false;
  }
}

export function closeProject() {
  setState({ pid: null, project: null, undo: EMPTY_UNDO, selected: [], focus: null, editSel: null, editRange: null, view: "home" });
}

export async function createProject({ name, settings, starter = false, style = null }) {
  try {
    const { project } = await api.post("/api/projects", { name, settings, starter });
    if (style && (style.preset || style.prompt || style.negative)) {
      await api.post(`/api/projects/${enc(project.id)}/ops`, {
        ops: [{ op: "project.update", patch: { style } }], undoable: false,
      });
    }
    await openProject(project.id, "board");
    return project;
  } catch (err) {
    toastError(err, "無法建立專案");
    return null;
  }
}

export async function duplicateProject(pid) {
  try {
    const { project } = await api.post(`/api/projects/${enc(pid)}/duplicate`);
    toast(`已建立「${project.name}」`, { level: "success", action: { label: "開啟", fn: () => openProject(project.id) } });
    return project;
  } catch (err) {
    toastError(err, "無法複製專案");
    return null;
  }
}

// Bring a .zip made by 打包專案 back in as a new project, then open it.
export async function importProjectZip(file) {
  if (!/\.zip$/i.test(file.name)) {
    toast(`只能匯入 Video Console 打包的 ZIP 檔：${file.name}`, { level: "warn" });
    return null;
  }
  const id = toast(`正在上傳 ${file.name}…`, { duration: 0, progress: 0 });
  try {
    const res = await upload(`/api/projects/import?name=${enc(file.name)}`, file, (p) => {
      updateToast(id, { message: p < 1 ? `正在上傳 ${file.name}…` : `正在解開 ${file.name}…`, progress: p < 1 ? p : null });
    });
    dismissToast(id);
    const note = res.dropped ? `；這是交付用的打包檔，未選用的 ${res.dropped} 個版本或素材不在包內` : "";
    toast(`已匯入「${res.project.name}」${note}`, { level: "success", duration: res.dropped ? 8000 : null });
    await openProject(res.project.id);
    return res.project;
  } catch (err) {
    dismissToast(id);
    toastError(err, "匯入專案失敗");
    return null;
  }
}

export function pickProjectZip() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".zip,application/zip";
  input.onchange = () => input.files[0] && importProjectZip(input.files[0]);
  input.click();
}

async function restoreDeleted(pid) {
  try {
    const trash = await api.get("/api/trash");
    const hit = trash.filter((t) => t.id === pid).sort((a, b) => b.deleted - a.deleted)[0];
    if (!hit) throw new Error("垃圾桶裡找不到這個專案");
    await api.post(`/api/trash/${enc(hit.folder)}/restore`);
    toast(`已還原「${hit.name}」`, { level: "success", action: { label: "開啟", fn: () => openProject(pid) } });
  } catch (err) {
    toastError(err, "無法還原專案");
  }
}

export async function deleteProject(pid, name) {
  // Files in the shared export folder are listed by full path; those in the project's own exports/ go to the
  // trash with it.
  const shared = await api.get(`/api/projects/${enc(pid)}/exports`)
    .then((list) => list.filter((e) => !e.path.startsWith("exports/")), () => []);
  const size = shared.reduce((sum, e) => sum + e.size, 0);
  const ok = await confirmDialog({
    title: `刪除「${name}」？`,
    message: "專案會移到垃圾桶，可以從專案頁的垃圾桶還原。",
    check: shared.length ? `一併刪除匯出資料夾裡這個專案的 ${shared.length} 個輸出檔（${fmtBytes(size)}，刪了無法還原）` : "",
    confirmLabel: "移到垃圾桶",
    danger: true,
  });
  if (!ok) return false;
  try {
    const res = await api.del(`/api/projects/${enc(pid)}${ok.checked ? "?exports=1" : ""}`);
    if (getState().pid === pid) closeProject();
    const files = res && res.removedExports ? `，並刪除 ${res.removedExports} 個匯出檔` : "";
    const busy = res && res.exportsInUse ? `（${res.exportsInUse} 個檔案使用中，沒有刪除）` : "";
    toast(`已將「${name}」移到垃圾桶${files}${busy}`, { action: { label: "復原", fn: () => restoreDeleted(pid) } });
    return true;
  } catch (err) {
    toastError(err, "無法刪除專案");
    return false;
  }
}

function ensureRev(pid, rev) {
  // The SSE event normally arrives first; if it was lost, fetch the document instead.
  setTimeout(() => {
    const s = getState();
    if (s.pid === pid && (!s.project || s.project.rev < rev)) loadProject(pid).catch(() => {});
  }, 1500);
}

// link: true sends the shot edits linked whatever the timeline toggle says (ripple delete); false never shifts
// or holds other items (a lift leaves an empty shot of the same length); null follows the toggle.
export async function ops(list, { label = "", coalesce = null, undoable = true, batch = null, link = null } = {}) {
  const { pid } = getState();
  if (!pid || !list.length || watchRefuses()) return null;
  try {
    const sent = link || (link === null && linking(list)) ? [{ op: "linked", ops: list }] : holding(list) ? [{ op: "linked", ops: list, shift: false }] : list;
    const res = await api.post(`/api/projects/${enc(pid)}/ops`, { ops: sent, label, coalesce, undoable, batch });
    if (res && res.changed) ensureRev(pid, res.rev);
    return res;
  } catch (err) {
    toastError(err, "變更沒有儲存");
    loadProject(pid).catch(() => {});
    return null;
  }
}

export function updateProject(patch, { label = "修改專案", coalesce = null } = {}) {
  return ops([{ op: "project.update", patch }], { label, coalesce });
}

// A named edit (POST /edit, app/vc/edits.py): the server works out the ops, the same ones Claude's edits get.
// Resolves to the response ({label, changed, by, id, level, partial, job}) or null once the failure is shown;
// an edit the server refuses (playhead too near an edge, a clip with no sound...) shows as a warning.
export async function editCmd(data, { coalesce = null } = {}) {
  const { pid } = getState();
  if (!pid || watchRefuses()) return null;
  try {
    const link = readPref(LINK_PREF, true, (v) => typeof v === "boolean");
    const res = await api.post(`/api/projects/${enc(pid)}/edit`, { ...data, link, coalesce });
    if (res && res.changed) ensureRev(pid, res.rev);
    return res;
  } catch (err) {
    if (err && err.status === 400) toast(err.message, { level: "warn" });
    else toastError(err, "變更沒有儲存");
    return null;
  }
}

async function step(kind) {
  const { pid, undo: u } = getState();
  if (!pid || watchRefuses()) return;
  const label = kind === "undo" ? u.undoLabel : u.redoLabel;
  if (!(kind === "undo" ? u.undo : u.redo)) {
    toast(kind === "undo" ? "沒有可以復原的步驟" : "沒有可以重做的步驟", { duration: 1600 });
    return;
  }
  try {
    // a quick second press reads the label before the first step's state arrives; the server knows which step it took
    const res = await api.post(`/api/projects/${enc(pid)}/${kind}`);
    toast(`${kind === "undo" ? "已復原" : "已重做"}：${res?.label || label || "上一步"}`, { duration: 1600 });
  } catch (err) {
    toastError(err);
  }
}

export const undo = () => step("undo");
export const redo = () => step("redo");

// ---------------------------------------------------------------- shots
export async function addShot({ index = null, patch = {}, select = true } = {}) {
  const s = getState();
  if (!s.project) return null;
  let at = index;
  if (at == null && s.focus) {
    const i = s.project.shots.findIndex((x) => x.id === s.focus);
    if (i >= 0) at = i + 1;
  }
  const shot = { id: newId("s"), ...patch };
  const res = await ops([{ op: "shot.add", shot, index: at }], { label: "新增鏡頭" });
  if (res && select) setState({ selected: [shot.id], focus: shot.id, inspectorTab: "content" });
  return res ? shot.id : null;
}

export function updateShot(id, patch, { coalesce = null, label = "修改鏡頭" } = {}) {
  return ops([{ op: "shot.update", id, patch }], { label, coalesce });
}

export function updateShots(ids, patch, label = "批次修改鏡頭") {
  return ops(ids.map((id) => ({ op: "shot.update", id, patch })), { label });
}

// ripple: the other tracks close up too, as with linking on.
export async function deleteShots(ids, { ripple = false } = {}) {
  const s = getState();
  if (!s.project || !ids.length) return;
  const shots = s.project.shots;
  if ((s.settings.ui || {}).confirmDelete !== false) {
    const i = shots.findIndex((x) => x.id === ids[0]);
    const held = shots.some((x) => ids.includes(x.id) && x.parked);
    const ok = await confirmDialog({
      title: ids.length > 1 ? `刪除 ${ids.length} 個鏡頭？` : `刪除「${shotLabel(shots[i], i)}」？`,
      message: `${ripple || linking([{ op: "shot.delete" }]) ? "整句落在鏡頭上的字幕，以及鏡頭上方的音訊與上層畫面會一起刪除，" : ""}${held ? "移出序列時收起的字幕與音訊也會一起刪除，" : ""}已生成的版本會留在專案裡，${undoKeyText()}可以復原。`,
      confirmLabel: "刪除",
      danger: true,
    });
    if (!ok) return;
  }
  const first = shots.findIndex((x) => ids.includes(x.id));
  const remaining = shots.filter((x) => !ids.includes(x.id));
  const next = remaining[Math.min(Math.max(first, 0), remaining.length - 1)];
  const res = await ops(ids.map((id) => ({ op: "shot.delete", id })), {
    label: ids.length > 1 ? `刪除 ${ids.length} 個鏡頭` : "刪除鏡頭",
    link: ripple || null,
  });
  if (res) setState({ selected: next ? [next.id] : [], focus: next ? next.id : null });
}

export async function duplicateShots(ids) {
  const s = getState();
  if (!s.project || !ids.length) return;
  const shots = s.project.shots;
  const picked = shots.map((shot, i) => ({ shot, i })).filter((x) => ids.includes(x.shot.id));
  const copies = [];
  // Insert from the back so earlier indexes stay valid while the server applies the list in order.
  const list = [...picked].reverse().map(({ shot, i }) => {
    const { parked, ...rest } = shot; // what the original holds stays with it
    const copy = { ...rest, id: newId("s"), name: shot.name ? `${shot.name} 副本` : "" };
    copies.unshift(copy.id);
    return { op: "shot.add", shot: copy, index: i + 1 };
  });
  const res = await ops(list, { label: picked.length > 1 ? `複製 ${picked.length} 個鏡頭` : "複製鏡頭" });
  if (res) setState({ selected: copies, focus: copies[0] });
}

export function moveShot(id, index) {
  return ops([{ op: "shot.move", id, index }], { label: "調整鏡頭順序" });
}

// Move several shots as one block, kept in story order, so the first of them lands at `index`.
// One shot.move per shot that is out of place, all in one undo step.
export function moveShots(ids, index) {
  const shots = (getState().project || {}).shots || [];
  const block = shots.filter((x) => ids.includes(x.id));
  const rest = shots.filter((x) => !ids.includes(x.id));
  const at = Math.max(0, Math.min(rest.length, index));
  const want = [...rest.slice(0, at), ...block, ...rest.slice(at)].map((x) => x.id);
  const now = shots.map((x) => x.id);
  const list = [];
  want.forEach((id, i) => {
    if (now[i] === id) return;
    now.splice(now.indexOf(id), 1);
    now.splice(i, 0, id);
    list.push({ op: "shot.move", id, index: i });
  });
  return list.length ? ops(list, { label: `移動 ${block.length} 個鏡頭` }) : Promise.resolve(null);
}

// ---------------------------------------------------------------- navigation
export function navigate(view, extra = {}) {
  setState({ view, ...extra });
}

export function focusShot(id, { tab = null, view = null } = {}) {
  setState({
    focus: id,
    selected: [id],
    ...(tab ? { inspectorTab: tab } : {}),
    ...(view ? { view } : {}),
  });
}

// A click on a timeline track's shot (`ids`: that track's shots in order). A plain click selects it alone,
// Ctrl adds or removes it, Shift selects every shot from the anchor to it: the last shot clicked without
// Shift while it is still selected, else the focused one.
let clickAnchor = null;
export function clickShot(e, id, ids, tab = null) {
  const s = getState();
  const from = ids.indexOf(clickAnchor && s.selected.includes(clickAnchor) ? clickAnchor : s.focus);
  const to = ids.indexOf(id);
  const switchTab = tab ? { inspectorTab: tab } : {};
  if (e.shiftKey && from >= 0 && to >= 0) {
    const [a, b] = from < to ? [from, to] : [to, from];
    setState({ editSel: { type: "shot", id }, focus: id, selected: ids.slice(a, b + 1), ...switchTab });
    return;
  }
  clickAnchor = id;
  if (e.ctrlKey || e.metaKey) {
    const has = s.selected.includes(id);
    const selected = has ? s.selected.filter((x) => x !== id) : [...s.selected, id];
    const focus = has ? selected[selected.length - 1] || null : id;
    setState({ editSel: focus ? { type: "shot", id: focus } : null, focus, selected, ...switchTab });
    return;
  }
  setState({ editSel: { type: "shot", id } });
  focusShot(id, { tab });
}

export async function revealJob(jobId) {
  const s = getState();
  const job = s.jobs.find((j) => j.id === jobId);
  if (!job) return navigate("queue");
  if (job.pid && job.pid !== s.pid && !(await openProject(job.pid))) return undefined;
  if (job.kind === "export" || job.kind === "package") return navigate("library", { libraryTab: "exports" });
  const project = getState().project;
  if (job.shotId && project && project.shots.some((x) => x.id === job.shotId)) {
    const view = getState().view === "edit" ? "edit" : "board";
    return focusShot(job.shotId, { tab: job.status === "failed" ? "generate" : "takes", view });
  }
  if (job.kind === "ingest") return navigate("library", { libraryTab: "media" });
  return navigate("queue");
}

// ---------------------------------------------------------------- settings / secrets / providers
export async function saveSettings(partial) {
  try {
    const saved = await api.put("/api/settings", partial);
    applyTheme(saved);
    setState({ settings: saved });
    return saved;
  } catch (err) {
    toastError(err, "設定沒有儲存");
    return null;
  }
}

export async function setSecret(name, value) {
  try {
    const secrets = await api.put("/api/secrets", { name, value });
    setState({ secrets });
    return true;
  } catch (err) {
    toastError(err, "金鑰沒有儲存");
    return false;
  }
}

export async function saveProvider(id, config) {
  try {
    const info = await api.put(`/api/providers/${enc(id)}`, { config });
    setState((s) => ({ providers: s.providers.map((p) => (p.id === id ? info : p)) }));
    return info;
  } catch (err) {
    toastError(err, "供應商設定沒有儲存");
    return null;
  }
}

export async function revealInFolder(path = "") {
  const { pid } = getState();
  if (!pid) return;
  try {
    await api.post(`/api/projects/${enc(pid)}/reveal`, path ? { path } : {});
  } catch (err) {
    toastError(err, "無法開啟資料夾");
  }
}

// The Windows select-folder dialog, opened by the server; resolves to the folder picked, or null when canceled.
export async function pickSystemFolder(start, title) {
  try {
    const res = await api.post("/api/system/pick-folder", { start, title });
    return (res && res.path) || null;
  } catch (err) {
    toastError(err, "無法開啟選擇資料夾視窗");
    return null;
  }
}

export async function revealSystemFolder(what) {
  try {
    await api.post("/api/system/reveal", { what });
  } catch (err) {
    toastError(err, "無法開啟資料夾");
  }
}
