// The shared folder every project's videos and ZIP files go to (settings export.folder, default
// <data>/exports), shown as a full path with buttons that change it and open it.
import { html, useState } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { revealSystemFolder, pickSystemFolder, saveSettings } from "../lib/actions.js";
import { Button, IconButton } from "./ui.js";
import { Icon } from "./icons.js";
import { toast } from "./toast.js";

export function defaultExportFolder(dataDir) {
  if (!dataDir) return "";
  return `${dataDir}${dataDir.includes("\\") || !dataDir.includes("/") ? "\\" : "/"}exports`;
}

export function useExportFolder() {
  return useStore((s) => (s.settings.export && s.settings.export.folder) || defaultExportFolder(s.system && s.system.dataDir));
}

export function ExportFolderNote({ label = "存到", class: cls = "" }) {
  const folder = useExportFolder();
  const [picking, setPicking] = useState(false);
  const change = async () => {
    setPicking(true);
    const picked = await pickSystemFolder(folder, "選擇匯出資料夾");
    const saved = picked && picked !== folder && await saveSettings({ export: { folder: picked } });
    setPicking(false);
    if (saved) toast(`匯出資料夾改為 ${picked}`);
  };
  return html`<span class=${`export-folder dim ${cls}`} title=${folder ? `${label} ${folder}` : ""}>
    <${Icon} name="folder" size=${14} /><span class="export-folder-label">${label}</span>
    <code class="export-folder-path"><bdi>${folder || "匯出資料夾"}</bdi></code>
    <${Button} size="sm" kind="ghost" busy=${picking} title="用 Windows 的選擇資料夾視窗挑選；所有專案之後的輸出都存到新資料夾，舊檔案留在原處"
      onClick=${change}>變更<//>
    <${IconButton} icon="folder-open" size=${15} title="開啟匯出資料夾" onClick=${() => revealSystemFolder("exports")} />
  </span>`;
}
