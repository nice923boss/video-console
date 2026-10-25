// Server-Sent Events keep the store in sync with the backend. The connection also keeps the server
// alive: it exits on its own after a while with no open page and no work left.
import { getState, setState } from "./store.js";
import { applyTheme } from "./theme.js";
import { handleNotify } from "./notify.js";
import { bootstrap, pruneSelection } from "./actions.js";
import { toast } from "../components/toast.js";

let source = null;
let reopenTimer = null;
let stopped = false;
let pageBuild = null; // the server build this page was loaded from

// A reconnect can reach a newer server that replaced the one this page came from (the launcher or vc_client
// swaps an outdated copy); the page keeps running its old scripts until it is reloaded.
function onHello(data) {
  const build = data && data.build;
  if (!pageBuild) pageBuild = build;
  else if (build && build !== pageBuild) {
    toast("Video Console 已換成新版，重新整理後才會用到新版的畫面與功能。", {
      id: "new-build", level: "warn", title: "已更新", duration: 0,
      action: { label: "重新整理", fn: () => location.reload() },
    });
  }
  bootstrap();
}

function upsert(list, item) {
  const i = list.findIndex((x) => x.id === item.id);
  if (i < 0) return [...list, item];
  const next = list.slice();
  next[i] = item;
  return next;
}

function onProject(data) {
  const s = getState();
  const project = data && data.project;
  if (!project || project.id !== s.pid) return;
  if (s.project && project.rev < s.project.rev) return; // an older snapshot arrived late
  setState({
    project,
    undo: { undo: data.undo, redo: data.redo, undoLabel: data.undoLabel, redoLabel: data.redoLabel },
    ...pruneSelection(s, project),
  });
}

const HANDLERS = {
  hello: onHello,
  resync: () => bootstrap(),
  settings: (data) => {
    applyTheme(data);
    setState({ settings: data });
  },
  project: onProject,
  projects: (data) => setState({ projects: data || [] }),
  job: (data) => data && setState((s) => ({ jobs: upsert(s.jobs, data) })),
  jobs: (data) => setState({ jobs: data || [] }),
  queue: (data) => data && setState({ queue: data }),
  comfy: (data) => setState({ comfy: data || [] }),
  workflows: (data) => setState({ workflows: data || [] }),
  notify: (data) => data && handleNotify(data),
  deps: (data) => data && setState({ deps: data }),
  system: (data) => data && setState({ system: data }),
  costs: (data) => data && setState({ costs: data }),
};

function onMessage(e) {
  let msg;
  try {
    msg = JSON.parse(e.data);
  } catch {
    return;
  }
  const fn = msg && HANDLERS[msg.type];
  if (fn) fn(msg.data);
}

export function connectEvents() {
  if (stopped) return;
  clearTimeout(reopenTimer);
  if (source) source.close();
  source = new EventSource("/api/events");
  source.onopen = () => setState({ connected: true });
  source.onmessage = onMessage;
  source.onerror = () => {
    setState({ connected: false });
    if (getState().boot === "loading") {
      setState({ boot: "error", bootError: "無法連線到 Video Console 伺服器，正在重試…" });
    }
    // CONNECTING means the browser retries by itself; CLOSED needs a manual reopen.
    if (source && source.readyState === EventSource.CLOSED) {
      reopenTimer = setTimeout(connectEvents, 3000);
    }
  };
}

// After an intentional shutdown: stop reconnecting so the page shows the "closed" screen.
export function stopEvents() {
  stopped = true;
  clearTimeout(reopenTimer);
  if (source) source.close();
  source = null;
  setState({ connected: false });
}
