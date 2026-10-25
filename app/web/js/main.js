// Entry point: mounts the app shell, keeps the URL hash and window title in step with the store, and
// owns the shortcuts that work on every page. The SSE "hello" event runs the first bootstrap.
import { html, render, useEffect, useState } from "./lib/h.js";
import { getState, setState, subscribe, useStore } from "./lib/store.js";
import { connectEvents } from "./lib/events.js";
import { navigate, undo, redo, importProjectZip } from "./lib/actions.js";
import { readWatch } from "./lib/watch.js";
import { watchErrors } from "./lib/crash-report.js";
import { useKeymap, allCommands, PALETTE_GROUP } from "./lib/keymap.js";
import { buildCommands } from "./commands.js";
import { Rail, TopBar, StatusBar, VIEWS, VIEW_LABEL } from "./components/shell.js";
import { Icon } from "./components/icons.js";
import { Button } from "./components/ui.js";
import { OverlayHost } from "./components/overlay.js";
import { ToastHost, toast } from "./components/toast.js";
import { openPalette } from "./components/palette.js";
import { openNewProject } from "./dialogs/new-project.js";
import { openExportDialog } from "./dialogs/export-dialog.js";
import { openShortcuts } from "./dialogs/shortcuts.js";
import { HomeView } from "./views/home.js";
import { BoardView } from "./views/board.js";
import { EditView } from "./views/edit.js";
import { LibraryView } from "./views/library.js";
import { QueueView } from "./views/queue.js";
import { EnginesView } from "./views/engines.js";
import { SettingsView } from "./views/settings.js";

const VIEW_COMPONENTS = {
  home: HomeView, board: BoardView, edit: EditView, library: LibraryView,
  queue: QueueView, engines: EnginesView, settings: SettingsView,
};

// Shared with the rail, the top bar and the command palette.
const commands = {
  palette: () => openPalette(buildCommands(commands)),
  shortcuts: () => openShortcuts(),
  newProject: () => openNewProject(),
  export: () => openExportDialog("video"),
};

function viewFromHash() {
  let id = "";
  try {
    id = decodeURIComponent(location.hash.slice(1));
  } catch {
    return null;
  }
  return VIEW_COMPONENTS[id] ? id : null;
}

// A palette command bound to a key runs as if picked in the palette; one that is hidden or disabled
// right now lets the key through.
function runCommand(id) {
  const c = buildCommands(commands).find((x) => x.id === id);
  if (!c || c.disabled) return false;
  c.run();
  return undefined;
}

function GlobalKeys() {
  const handlers = {
    "app.palette": { fn: commands.palette, allowInInput: true },
    "app.undo": () => (getState().pid ? undo() : false),
    "app.redo": () => (getState().pid ? redo() : false),
    "help:keys": commands.shortcuts,
  };
  VIEWS.forEach((v) => {
    handlers[`view:${v.id}`] = () => navigate(v.id);
  });
  for (const c of allCommands()) {
    if (c.group === PALETTE_GROUP && !handlers[c.id]) handlers[c.id] = () => runCommand(c.id);
  }
  useKeymap(handlers);
  return null;
}

function BootScreen() {
  return html`<div class="boot-screen">
    <div class="boot-mark">VC</div>
    <div class="boot-text">正在啟動 Video Console…</div>
  </div>`;
}

function BootError() {
  const message = useStore((s) => s.bootError);
  const retry = () => {
    setState({ boot: "loading", bootError: null });
    connectEvents();
  };
  return html`<div class="boot-screen">
    <div class="boot-card" role="alert">
      <div class="boot-mark">VC</div>
      <h1>無法連線到 Video Console</h1>
      ${message && html`<p>${message}</p>`}
      <p class="dim">背景伺服器可能已經結束。請重新執行 Video Console.exe（原始碼版是「啟動 Video Console.bat」），再按重試。</p>
      <${Button} kind="primary" icon="refresh" onClick=${retry}>重試<//>
    </div>
  </div>`;
}

function Closed() {
  const [stuck, setStuck] = useState(false);
  useEffect(() => {
    document.title = "Video Console（已結束）";
  }, []);
  const close = () => {
    window.close();
    setTimeout(() => setStuck(true), 300); // still here: the browser refused to close a window it did not open
  };
  return html`<div class="boot-screen">
    <div class="boot-card">
      <div class="boot-mark"><${Icon} name="power" size=${22} /></div>
      <h1>Video Console 已結束</h1>
      <p class="dim">背景伺服器已停止，排隊中的工作會保留到下次啟動。要再使用時，執行 Video Console.exe（原始碼版是「啟動 Video Console.bat」）。</p>
      <${Button} icon="x" onClick=${close}>關閉視窗<//>
      ${stuck && html`<p class="dim">瀏覽器不允許網頁自己關閉視窗，請按右上角的 ✕ 或 Alt+F4。</p>`}
    </div>
  </div>`;
}

function Workspace() {
  const view = useStore((s) => s.view);
  const connected = useStore((s) => s.connected);
  const View = VIEW_COMPONENTS[view] || HomeView;
  return html`<div class="app">
    <${GlobalKeys} />
    <${Rail} commands=${commands} />
    <div class="app-body">
      <${TopBar} commands=${commands} />
      ${!connected && html`<div class="conn-banner" role="status">
        <${Icon} name="wifi-off" size=${15} />
        <span>與本機伺服器的連線中斷，正在重新連線。這段時間的修改可能沒有儲存。</span>
      </div>`}
      <main class="app-main"><${View} key=${view} /></main>
      <${StatusBar} />
    </div>
  </div>`;
}

const SCREENS = { loading: BootScreen, error: BootError, closed: Closed, ready: Workspace };

function Root() {
  const boot = useStore((s) => s.boot);
  const Screen = SCREENS[boot] || BootScreen;
  return html`<${Screen} />
    <${OverlayHost} />
    <${ToastHost} />`;
}

// URL hash and window title follow the store once the app is up.
function syncLocation(s) {
  if (s.boot !== "ready") return;
  const hash = `#${s.view}`;
  if (location.hash !== hash) {
    if (location.hash) history.pushState(null, "", hash);
    else history.replaceState(null, "", hash);
  }
  const page = VIEW_LABEL[s.view] || "";
  const title = s.project ? `${s.project.name}｜${page}｜Video Console` : `${page}｜Video Console`;
  if (document.title !== title) document.title = title;
}

window.addEventListener("popstate", () => {
  const view = viewFromHash();
  if (view && view !== getState().view) setState({ view });
});

// Files dropped outside a drop zone would make the window navigate to them; a .zip dropped there
// is imported as a project instead.
window.addEventListener("dragover", (e) => {
  if (!e.defaultPrevented && e.dataTransfer) {
    e.dataTransfer.dropEffect = e.dataTransfer.types.includes("Files") ? "copy" : "none";
  }
  e.preventDefault();
});
window.addEventListener("drop", (e) => {
  if (e.defaultPrevented) return;
  e.preventDefault();
  const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
  if (!files.length) return;
  const zip = files.find((f) => /\.zip$/i.test(f.name));
  if (zip) importProjectZip(zip);
  else toast("這裡只能放「打包專案」的 ZIP 檔來匯入；素材請拖到素材庫或鏡頭卡片上", { level: "warn" });
});

const initial = viewFromHash();
if (initial) setState({ view: initial });
if (readWatch()) setState({ watch: true });
subscribe(syncLocation);

const root = document.getElementById("app");
root.textContent = "";
watchErrors();
render(html`<${Root} />`, root);
connectEvents();
