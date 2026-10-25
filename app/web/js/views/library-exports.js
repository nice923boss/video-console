// Exports tab of the library: rendered videos and project packages on disk, the jobs still producing
// them, and quick downloads of the storyboard, captions and shot list.
import { html, useState, useEffect } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { revealInFolder, revealSystemFolder } from "../lib/actions.js";
import { fileUrl, fmtBytes, fmtRelTime, fmtDateTime } from "../lib/format.js";
import { Button, IconButton, Empty, Spinner, Section } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { openLightbox } from "../components/media.js";
import { confirmDialog } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { JobRow } from "../components/job-row.js";
import { useExportFolder } from "../components/export-folder.js";
import { openExportDialog } from "../dialogs/export-dialog.js";
import { pickReviewFile } from "../lib/review.js";

const enc = encodeURIComponent;
const KIND_ICON = { video: "film", image: "image", audio: "music", package: "package", file: "file" };
const KIND_NAME = { video: "影片", image: "圖片", audio: "音訊", package: "打包檔", file: "檔案" };
const PLAYABLE = new Set(["video", "image", "audio"]);
const isExportJob = (j, pid) => j.pid === pid && (j.kind === "export" || j.kind === "package");
const isActive = (j) => j.status === "queued" || j.status === "running";

const asEntry = (f) => ({ id: f.name, kind: f.kind, file: f.path, name: f.name, created: f.modified });

function DocLinks({ project }) {
  const base = `/api/projects/${enc(project.id)}`;
  const captions = (project.captions || []).length;
  return html`<div class="exp-docs">
    <a class="exp-doc" href=${`${base}/storyboard`} target="_blank" rel="noopener" title="在新視窗開啟，可用瀏覽器列印成 PDF">
      <${Icon} name="grid" size=${18} />
      <span><b>分鏡表</b><small>網頁版，可列印成 PDF</small></span>
      <${Icon} name="external" size=${14} class="dim" />
    </a>
    ${captions
      ? html`<a class="exp-doc" href=${`${base}/captions.srt`} download title="SubRip 字幕檔，可匯入其他剪輯軟體">
          <${Icon} name="captions" size=${18} />
          <span><b>字幕檔 SRT</b><small>${captions} 則字幕</small></span>
          <${Icon} name="download" size=${14} class="dim" />
        </a>`
      : html`<div class="exp-doc is-disabled" title="先在剪輯頁加上字幕">
          <${Icon} name="captions" size=${18} />
          <span><b>字幕檔 SRT</b><small>還沒有字幕</small></span>
        </div>`}
    <a class="exp-doc" href=${`${base}/shots.csv`} download title="每個鏡頭一列，含描述、長度與字幕，可用 Excel 開啟">
      <${Icon} name="list" size=${18} />
      <span><b>鏡頭清單 CSV</b><small>${project.shots.length} 個鏡頭</small></span>
      <${Icon} name="download" size=${14} class="dim" />
    </a>
    <a class="exp-doc" href=${`${base}/review.html`} download title="單一網頁檔，傳給客戶用瀏覽器開啟，逐鏡留言後下載意見檔傳回來">
      <${Icon} name="message" size=${18} />
      <span><b>客戶審稿網頁</b><small>單檔網頁，客戶逐鏡留言</small></span>
      <${Icon} name="download" size=${14} class="dim" />
    </a>
    <a class="exp-doc" href="#" role="button" title="選擇客戶從審稿網頁下載的 .json 意見檔"
        onClick=${(e) => { e.preventDefault(); pickReviewFile(); }}>
      <${Icon} name="upload" size=${18} />
      <span><b>匯入審稿意見</b><small>意見會出現在各鏡頭的屬性</small></span>
    </a>
  </div>`;
}

function ExportRow({ pid, file, onPlay, onDelete }) {
  const playable = PLAYABLE.has(file.kind);
  return html`<div class=${`exp-row kind-${file.kind}`}>
    <span class="exp-icon"><${Icon} name=${KIND_ICON[file.kind] || "file"} size=${18} /></span>
    <div class="exp-info">
      <span class="exp-name" title=${file.name}>${file.name}</span>
      <span class="exp-meta dim">
        ${KIND_NAME[file.kind] || "檔案"}<i></i>${fmtBytes(file.size)}<i></i>
        <span title=${fmtDateTime(file.modified)}>${fmtRelTime(file.modified)}</span>
      </span>
    </div>
    <div class="exp-actions">
      ${playable && html`<${IconButton} icon=${file.kind === "image" ? "eye" : "play"} title="預覽" onClick=${() => onPlay(file)} />`}
      <a class="icon-btn" href=${fileUrl(pid, file.path, { download: true })} download title="下載" aria-label="下載">
        <${Icon} name="download" size=${18} />
      </a>
      <${IconButton} icon="folder-open" title="在檔案總管中顯示" onClick=${() => revealInFolder(file.path)} />
      <${IconButton} icon="trash" title="刪除檔案" onClick=${() => onDelete(file)} />
    </div>
  </div>`;
}

export function ExportsPane({ project }) {
  const pid = project.id;
  const jobs = useStore((s) => s.jobs);
  const folder = useExportFolder();
  const [files, setFiles] = useState(null);
  const [error, setError] = useState("");
  const mine = jobs.filter((j) => isExportJob(j, pid));
  const active = mine.filter(isActive);
  // Reload the list whenever one of this project's export or package jobs changes state.
  const signature = mine.map((j) => `${j.id}:${j.status}`).join(",");

  const load = () => api.get(`/api/projects/${enc(pid)}/exports`).then(
    (list) => {
      setFiles(list);
      setError("");
    },
    (err) => setError(err.message || "無法讀取輸出檔清單"),
  );
  useEffect(() => {
    load();
  }, [pid, signature]);

  const play = (file) => {
    const list = (files || []).filter((f) => PLAYABLE.has(f.kind));
    openLightbox(pid, list.map(asEntry), Math.max(0, list.indexOf(file)));
  };
  const remove = async (file) => {
    const ok = await confirmDialog({ title: "刪除輸出檔", danger: true, confirmLabel: "刪除",
      message: `「${file.name}」（${fmtBytes(file.size)}）會從電腦永久刪除，無法復原。專案內容不受影響，之後可以重新輸出。` });
    if (!ok) return;
    try {
      await api.del(`/api/projects/${enc(pid)}/exports/${enc(file.name)}`);
      setFiles((list) => (list || []).filter((f) => f.name !== file.name));
      toast(`已刪除「${file.name}」`);
    } catch (err) {
      toastError(err, "無法刪除檔案");
      load();
    }
  };

  const videos = (files || []).filter((f) => f.kind !== "package");
  const packages = (files || []).filter((f) => f.kind === "package");
  const total = (files || []).reduce((n, f) => n + f.size, 0);
  const rows = (list) => list.map((f) => html`<${ExportRow} key=${f.name} pid=${pid} file=${f} onPlay=${play} onDelete=${remove} />`);

  return html`<div class="lib-pane exp-pane">
    <div class="lib-toolbar">
      <div class="exp-lead">
        <b>輸出成品</b>
        <span class="dim">${files ? `${files.length} 個檔案，共 ${fmtBytes(total)}` : "讀取中…"}</span>
        <span class="dim exp-where" title=${folder}>存在<code class="export-folder-path"><bdi>${folder}</bdi></code></span>
      </div>
      <${IconButton} icon="refresh" title="重新整理" onClick=${load} />
      <${Button} icon="folder-open" title=${`開啟 ${folder}`} onClick=${() => revealSystemFolder("exports")}>開啟資料夾<//>
      <${Button} icon="package" onClick=${() => openExportDialog("package")}>打包專案<//>
      <${Button} kind="primary" icon="film" onClick=${() => openExportDialog("video")}>輸出影片<//>
    </div>
    <div class="lib-scroll exp-scroll">
      ${active.length > 0 && html`<${Section} title=${`進行中（${active.length}）`} icon="activity" class="exp-section">
        <div class="job-list">${active.map((j) => html`<${JobRow} key=${j.id} job=${j} project=${project} />`)}</div>
      <//>`}
      <${Section} title="影片與檔案" icon="film" class="exp-section">
        ${error
          ? html`<div class="note note-warn"><${Icon} name="alert" size=${15} /><span class="note-text">${error}</span></div>`
          : files == null
            ? html`<div class="exp-loading"><${Spinner} /><span>讀取中…</span></div>`
            : videos.length
              ? html`<div class="exp-list">${rows(videos)}</div>`
              : html`<${Empty} icon="film" title="還沒有輸出過影片"
                  actions=${html`<${Button} kind="primary" icon="film" onClick=${() => openExportDialog("video")}>輸出影片<//>`}>
                  輸出會把序列裡的鏡頭、轉場、音軌與字幕合成一支 MP4，存在匯出資料夾（設定 › 影片輸出可更改）。
                <//>`}
      <//>
      ${packages.length > 0 && html`<${Section} title="打包檔" icon="package" class="exp-section">
        <div class="exp-list">${rows(packages)}</div>
      <//>`}
      <${Section} title="文件" icon="file" class="exp-section">
        <${DocLinks} project=${project} />
      <//>
    </div>
  </div>`;
}
