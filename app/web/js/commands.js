// Command palette entries, rebuilt from the current state each time the palette opens.
import { getState } from "./lib/store.js";
import { navigate, focusShot, openProject, saveSettings, revealInFolder, addShot, pickProjectZip } from "./lib/actions.js";
import { generate, pauseQueue } from "./lib/jobs-actions.js";
import { currentTheme } from "./lib/theme.js";
import { shotLabel, fileUrl } from "./lib/format.js";
import { shotCover } from "./lib/sequence.js";
import { VIEWS } from "./components/shell.js";
import { openNewProject } from "./dialogs/new-project.js";
import { openExportDialog } from "./dialogs/export-dialog.js";
import { openShortcuts } from "./dialogs/shortcuts.js";
import { openEditHelp } from "./dialogs/edit-help.js";
import { openTour } from "./dialogs/tour.js";
import { openHistory } from "./dialogs/history-dialog.js";
import { openScriptImport } from "./dialogs/script-import.js";
import { openCastDialog } from "./dialogs/cast-dialog.js";
import { openCaptionCondense } from "./dialogs/caption-condense.js";
import { openTitleDialog } from "./dialogs/title-dialog.js";
import { downloadReviewPage, pickReviewFile } from "./lib/review.js";
import { generateMissing } from "./lib/voice.js";
import { defineCommands, keysOf } from "./lib/keymap.js";

const ENGINE_TABS = [
  { id: "routes", label: "路由", icon: "route", keywords: "route 引擎" },
  { id: "comfy", label: "ComfyUI 伺服器", icon: "server", keywords: "comfyui server 連線" },
  { id: "workflows", label: "工作流程範本", icon: "workflow", keywords: "workflow 範本 匯入" },
  { id: "providers", label: "雲端 API", icon: "cloud", keywords: "api key 金鑰 供應商 provider" },
  { id: "llm", label: "文字模型", icon: "bot", keywords: "llm openai 潤飾 劇本" },
  { id: "voice", label: "配音", icon: "mic", keywords: "audio8voice voai 雲端配音 配音 語音 tts" },
];

// Palette commands that can take a key in Settings (shots and projects come and go, so they stay out).
defineCommands([
  ...VIEWS.map((v, i) => ({ id: `view:${v.id}`, label: `前往${v.label}`, keys: [`alt+${i + 1}`] })),
  ...ENGINE_TABS.map((t) => ({ id: `engines:${t.id}`, label: `引擎：${t.label}` })),
  { id: "project:new", label: "新增專案" },
  { id: "project:starter", label: "從範例專案開始" },
  { id: "project:import", label: "匯入專案" },
  { id: "project:script", label: "從劇本拆解鏡頭" },
  { id: "project:history", label: "版本紀錄" },
  { id: "project:folder", label: "開啟專案資料夾" },
  { id: "export:video", label: "輸出影片" },
  { id: "export:package", label: "打包專案" },
  { id: "voice:cast", label: "人物表" },
  { id: "voice:generate", label: "生成缺少或過期的配音" },
  { id: "queue:pause", label: "暫停或繼續佇列" },
  { id: "theme:flip", label: "切換深淺色主題" },
  { id: "help:keys", label: "快捷鍵一覽", keys: ["?"] },
  { id: "help:edit", label: "剪輯功能說明" },
  { id: "help:tour", label: "新手導覽" },
]);

// Palette rows that show the key of a storyboard command.
const HINT_ALIAS = { "shot:add": "board.add", "shot:image": "board.image", "shot:video": "board.video" };

function pageCommands(pid) {
  const pages = VIEWS.map((v) => ({
    id: `view:${v.id}`, group: "頁面", label: `前往${v.label}`, icon: v.icon,
    disabled: !!v.project && !pid, detail: v.project && !pid ? "先開啟一個專案" : "", run: () => navigate(v.id),
  }));
  const engines = ENGINE_TABS.map((t) => ({
    id: `engines:${t.id}`, group: "頁面", label: `引擎：${t.label}`, icon: t.icon, keywords: t.keywords,
    run: () => navigate("engines", { enginesTab: t.id }),
  }));
  return [...pages, ...engines];
}

function projectCommands(s) {
  const has = !!s.project;
  const list = [
    { id: "project:new", group: "專案", label: "新增專案…", icon: "plus", run: () => openNewProject() },
    { id: "project:starter", group: "專案", label: "從範例專案開始…", icon: "sparkles", keywords: "sample 範例",
      run: () => openNewProject({ starter: true }) },
    { id: "project:import", group: "專案", label: "匯入專案…", icon: "upload", keywords: "zip 備份 還原 打包 import",
      run: () => pickProjectZip() },
    has && { id: "project:script", group: "專案", label: "從劇本拆解鏡頭…", icon: "file", keywords: "script 劇本 匯入",
      run: () => openScriptImport() },
    has && { id: "project:history", group: "專案", label: "版本紀錄…", icon: "history", keywords: "還原 快照 history",
      run: () => openHistory() },
    has && { id: "project:folder", group: "專案", label: "開啟專案資料夾", icon: "folder-open", run: () => revealInFolder() },
  ];
  const others = [...s.projects]
    .filter((p) => p.id !== s.pid)
    .sort((a, b) => (b.updated || 0) - (a.updated || 0))
    .map((p) => ({
      id: `open:${p.id}`, group: "專案", label: `開啟專案：${p.name}`, icon: "folder", detail: `${p.shots} 個鏡頭`,
      run: () => openProject(p.id),
    }));
  return [...list, ...others];
}

function shotCommands(s) {
  const { project, pid } = s;
  if (!project) return [];
  const targets = s.selected.length ? s.selected : s.focus ? [s.focus] : [];
  const n = targets.length;
  const what = n > 1 ? `選取的 ${n} 個鏡頭` : "目前的鏡頭";
  const actions = [
    { id: "shot:add", group: "鏡頭", label: "新增鏡頭", icon: "plus", run: () => addShot() },
    { id: "shot:image", group: "鏡頭", label: `${what}生圖`, icon: "image", disabled: !n,
      run: () => generate("image", targets) },
    { id: "shot:video", group: "鏡頭", label: `${what}生影片`, icon: "video", disabled: !n,
      run: () => generate("video", targets) },
  ];
  const view = s.view === "edit" ? "edit" : "board";
  const jumps = project.shots.map((shot, i) => {
    const cover = shotCover(project, shot);
    return {
      id: `shot:${shot.id}`, group: "鏡頭", label: shotLabel(shot, i), icon: "clapper",
      detail: (shot.prompt || "").slice(0, 80), thumb: cover ? fileUrl(pid, cover.path) : "",
      run: () => focusShot(shot.id, { view }),
    };
  });
  return [...actions, ...jumps];
}

function appCommands(s, commands) {
  const dark = currentTheme() === "dark";
  const paused = !!(s.queue && s.queue.paused);
  return [
    s.project && { id: "export:video", group: "輸出", label: "輸出影片…", icon: "film", run: commands.export },
    s.project && { id: "export:package", group: "輸出", label: "打包專案…", icon: "package", keywords: "zip 備份",
      run: () => openExportDialog("package") },
    s.project && { id: "review:page", group: "輸出", label: "下載客戶審稿網頁", icon: "message",
      keywords: "審稿 客戶 意見 留言 review comment html", run: () => downloadReviewPage() },
    s.project && { id: "review:import", group: "輸出", label: "匯入審稿意見…", icon: "upload",
      keywords: "審稿 客戶 意見 留言 review comment json", run: () => pickReviewFile() },
    s.project && { id: "voice:cast", group: "配音", label: "人物表…", icon: "users", keywords: "cast 人物 角色 聲音 audio8voice voai",
      run: () => openCastDialog() },
    s.project && { id: "voice:generate", group: "配音", label: "生成缺少或過期的配音", icon: "mic", keywords: "tts 語音 字幕 audio8voice voai",
      run: () => generateMissing() },
    s.project && { id: "captions:condense", group: "字幕", label: "濃縮過長的字幕…", icon: "captions",
      keywords: "字幕 濃縮 太長 每秒 閱讀 ai llm", run: () => openCaptionCondense() },
    s.project && { id: "captions:title", group: "字幕", label: "加入標題字卡…", icon: "type",
      keywords: "標題 字卡 片頭 下標 章節 lower third title 範本", run: () => openTitleDialog() },
    { id: "queue:pause", group: "佇列", label: paused ? "繼續佇列" : "暫停佇列", icon: paused ? "play" : "pause",
      run: () => pauseQueue(!paused) },
    { id: "theme:flip", group: "外觀", label: dark ? "切換成淺色主題" : "切換成深色主題", icon: dark ? "sun" : "moon",
      keywords: "theme 主題 dark light", run: () => saveSettings({ ui: { theme: dark ? "light" : "dark" } }) },
    { id: "help:keys", group: "說明", label: "快捷鍵一覽", icon: "keyboard", run: () => openShortcuts() },
    { id: "help:edit", group: "說明", label: "剪輯功能說明", icon: "book", keywords: "help 說明 分割 標記 關鍵幀 變速 倒放 定格 原聲 音量",
      run: () => openEditHelp() },
    { id: "help:tour", group: "說明", label: "新手導覽", icon: "book", keywords: "tour 導覽 教學 新手 入門 help",
      run: () => openTour() },
  ];
}

// commands: the app-level handlers shared with the rail and the top bar.
export function buildCommands(commands) {
  const s = getState();
  return [...shotCommands(s), ...projectCommands(s), ...pageCommands(s.pid), ...appCommands(s, commands)].filter(Boolean)
    .map((c) => ({ ...c, hint: keysOf(HINT_ALIAS[c.id] || c.id)[0] || "" }));
}
