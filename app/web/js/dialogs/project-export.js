// 匯出專案: the whole project as one ZIP (a "package" job with fullBackup) that 匯入專案 opens on another
// computer to keep editing. A one-off destination (a USB drive) does not change the shared export folder.
import { html, useState, useEffect } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { navigate, openProject, pickSystemFolder } from "../lib/actions.js";
import { submitJob } from "../lib/jobs-actions.js";
import { fmtBytes } from "../lib/format.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, Toggle } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { useExportFolder } from "../components/export-folder.js";
import { toast } from "../components/toast.js";

const enc = encodeURIComponent;
const p5Count = (project) => Object.values(project.media || {}).filter((m) => !m.trashed && m.p5 && m.p5.page).length;

function ProjectExport({ project, close }) {
  const shared = useExportFolder();
  const p5 = p5Count(project);
  const [withP5, setWithP5] = useState(true);
  const [latest, setLatest] = useState(false);
  const [folder, setFolder] = useState("");
  const [size, setSize] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api.get(`/api/projects/${enc(project.id)}/usage`).then((d) => live && setSize(d.used), () => {});
    return () => { live = false; };
  }, [project.id]);

  const choose = async () => {
    const picked = await pickSystemFolder(folder || shared, "選擇 ZIP 要存放的資料夾（例如隨身碟）");
    if (picked) setFolder(picked === shared ? "" : picked);
  };
  const submit = async () => {
    setBusy(true);
    const job = await submitJob("package", null, { fullBackup: true, includeVideos: true, includeExport: latest,
      includeP5: withP5, folder }, { quiet: true });
    setBusy(false);
    if (!job) return;
    toast(`已加入佇列：${job.title}`, {
      action: { label: "查看進度", fn: () => navigate("library", { libraryTab: "exports" }) },
    });
    close(job);
  };

  const footer = html`
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="download" busy=${busy} onClick=${submit}>匯出 ZIP<//>`;
  return html`<${Modal} title="匯出專案" width=${560} backdropClose=${false} onClose=${() => close(null)} footer=${footer}
      class="export-dialog">
    <p class="dim exp-lede">把「${project.name}」整個存成一個 ZIP。帶到另一台電腦，在 Video Console 按「匯入專案」選這個檔案，就能接著剪輯。ZIP 會包含：</p>
    <ul class="exp-contents">
      <li><${Icon} name="film" size=${15} />所有鏡頭、版本與影片</li>
      <li><${Icon} name="scissors" size=${15} />剪輯時間軸、字幕與配樂</li>
      <li><${Icon} name="images" size=${15} />匯入的素材</li>
      <li><${Icon} name="message" size=${15} />AI 助理文件與審稿紀錄</li>
      <li><${Icon} name="mic" size=${15} />人聲淨化後的音檔</li>
      ${p5 > 0 && html`<li><${Icon} name="code" size=${15} />p5 動畫原始碼</li>`}
    </ul>
    ${p5 > 0 && html`<${Toggle} checked=${withP5} onChange=${setWithP5} label="附上 p5 動畫原始碼"
      hint=${`${p5} 段影片連結到 p5 原始碼。附上後，另一台電腦找不到原本的資料夾時會改用包內的副本，一樣能改字重算；超過 2 GB 的資料夾不會放進去`} />`}
    <${Toggle} checked=${latest} onChange=${setLatest} label="附上最新一次輸出的完整影片"
      hint="成片可以在另一台電腦重新輸出，不附上可以省空間" />
    <${Field} label="ZIP 檔存到" hint=${size != null ? `專案素材目前約 ${fmtBytes(size)}，ZIP 大小相近` : null}
        after=${folder && html`<${Button} size="sm" kind="ghost" onClick=${() => setFolder("")}>改回匯出資料夾<//>`}>
      <div class="row">
        <code class="grow export-folder-path" title=${folder || shared}><bdi>${folder || shared || "匯出資料夾"}</bdi></code>
        <${Button} size="sm" icon="folder" onClick=${choose}
          title="只有這次存到別的地方（例如隨身碟），不會改變匯出資料夾">其他位置…<//>
      </div>
    <//>
  <//>`;
}

// Export `pid` (default: the open project) for another computer; resolves with the job, or null when canceled.
export async function openProjectExport(pid = null) {
  if (pid && pid !== getState().pid && !(await openProject(pid, "home"))) return null;
  const project = getState().project;
  if (!project) {
    toast("請先開啟要匯出的專案", { level: "warn" });
    return null;
  }
  return openModal((close) => html`<${ProjectExport} project=${project} close=${close} />`);
}
