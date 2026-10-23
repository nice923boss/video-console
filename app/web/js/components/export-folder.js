// The shared folder every project's videos and ZIP files go to (settings export.folder, default
// <data>/exports), shown as a full path with a button that opens it.
import { html } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { revealSystemFolder } from "../lib/actions.js";
import { IconButton } from "./ui.js";
import { Icon } from "./icons.js";

export function defaultExportFolder(dataDir) {
  if (!dataDir) return "";
  return `${dataDir}${dataDir.includes("\\") || !dataDir.includes("/") ? "\\" : "/"}exports`;
}

export function useExportFolder() {
  return useStore((s) => (s.settings.export && s.settings.export.folder) || defaultExportFolder(s.system && s.system.dataDir));
}

export function ExportFolderNote({ label = "存到", class: cls = "" }) {
  const folder = useExportFolder();
  return html`<span class=${`export-folder dim ${cls}`} title=${folder ? `${label} ${folder}` : ""}>
    <${Icon} name="folder" size=${14} /><span class="export-folder-label">${label}</span>
    <code class="export-folder-path"><bdi>${folder || "匯出資料夾"}</bdi></code>
    <${IconButton} icon="folder-open" size=${15} title="開啟匯出資料夾" onClick=${() => revealSystemFolder("exports")} />
  </span>`;
}
