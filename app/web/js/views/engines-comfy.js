// Engines page, ComfyUI tab: every configured server with its live health (version, GPU, memory, queue),
// server actions (re-check, free VRAM, interrupt, model list, template check) and the shared options.
import { html, useState, useEffect, useMemo } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveSettings, navigate } from "../lib/actions.js";
import { fmtBytes, fmtRelTime } from "../lib/format.js";
import { Button, IconButton, Field, NumberInput, Select, Toggle, Badge, Dot, Progress, Empty, Section, Spinner }
  from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { openModal, Modal, openMenu, confirmDialog } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { SecretField } from "../components/secret-field.js";
import { openServerDialog } from "../dialogs/comfy-server.js";

const NONE = [];
const enc = encodeURIComponent;
const FOLDER_LABEL = {
  checkpoints: "Checkpoint 模型", diffusion_models: "擴散模型", text_encoders: "文字編碼器",
  vae: "VAE", loras: "LoRA", upscale_models: "放大模型",
};

function stateOf(st, server) {
  if (server.enabled === false) return { tone: "neutral", label: "已停用" };
  if (!st || !st.checked) return { tone: "neutral", label: "尚未檢查" };
  return st.online ? { tone: "ok", label: "在線" } : { tone: "danger", label: "離線" };
}

function ModelsBody({ sid }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    api.get(`/api/comfy/${enc(sid)}/models`).then(setData, (err) => setError(err.message));
  }, [sid]);
  if (error) return html`<div class="note note-danger"><${Icon} name="x-circle" size=${15} /><span class="note-text">${error}</span></div>`;
  if (!data) return html`<div class="exp-loading"><${Spinner} /><span>讀取模型清單…</span></div>`;
  const needle = query.trim().toLowerCase();
  return html`
    <label class="search-box">
      <${Icon} name="search" size=${15} />
      <input type="search" placeholder="搜尋模型檔名" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
    </label>
    <div class="model-groups">
      ${Object.entries(data).map(([folder, names]) => {
        const shown = names.filter((n) => !needle || n.toLowerCase().includes(needle));
        return html`<section key=${folder} class="model-group">
          <h4>${FOLDER_LABEL[folder] || folder}<span class="dim mono">models/${folder}</span><span class="count">${names.length}</span></h4>
          ${shown.length
            ? html`<ul>${shown.map((n) => html`<li key=${n} class="mono">${n}</li>`)}</ul>`
            : html`<p class="dim">${names.length ? "沒有符合的檔案" : "沒有檔案"}</p>`}
        </section>`;
      })}
    </div>`;
}

function openModels(server) {
  return openModal((close) => html`<${Modal} title=${`「${server.name}」的模型`} width=${640} onClose=${() => close()}
      class="models-dialog" footer=${html`<${Button} onClick=${() => close()}>關閉<//>`}>
    <${ModelsBody} sid=${server.id} />
  <//>`);
}

async function refreshOne(sid) {
  try {
    const st = await api.post(`/api/comfy/${enc(sid)}/refresh`);
    setState((s) => ({ comfy: s.comfy.map((c) => (c.id === sid ? { ...c, ...st } : c)) }));
  } catch (err) {
    toastError(err, "無法檢查伺服器");
  }
}

async function serverAction(server, action) {
  if (action === "interrupt") {
    const ok = await confirmDialog({ title: "中斷目前的工作", danger: true, confirmLabel: "中斷",
      message: `「${server.name}」正在執行的工作會立刻停止，包含其他程式送去的工作。這個程式的工作會標成「已中斷」，可以重試。` });
    if (!ok) return;
  }
  try {
    await api.post(`/api/comfy/${enc(server.id)}/${action}`);
    toast(action === "free" ? `已請「${server.name}」卸載模型並釋放顯示卡記憶體` : `已中斷「${server.name}」目前的工作`);
    refreshOne(server.id);
  } catch (err) {
    toastError(err);
  }
}

function Meter({ label, free, total }) {
  if (!total) return null;
  const used = Math.max(0, total - (free || 0));
  const ratio = used / total;
  return html`<div class="cs-meter">
    <div class="cs-meter-top"><span>${label}</span><span class="mono">${fmtBytes(used)} / ${fmtBytes(total)}</span></div>
    <${Progress} value=${ratio} tone=${ratio > 0.9 ? "danger" : ratio > 0.75 ? "warn" : "accent"} />
  </div>`;
}

function ServerCard({ server, st, index, count, onMove, onToggle, onRemove }) {
  const [busy, setBusy] = useState(false);
  const state = stateOf(st, server);
  const online = st && st.online && server.enabled !== false;
  const dev = st && st.device;
  const recheck = async () => {
    setBusy(true);
    await refreshOne(server.id);
    setBusy(false);
  };
  const menu = (e) => openMenu(e, [
    { label: "模型清單", icon: "layers", disabled: !online, onClick: () => openModels(server) },
    { label: "檢查範本相容性", icon: "workflow", disabled: !online,
      onClick: () => navigate("engines", { enginesTab: "workflows", wfCheck: { sid: server.id, at: Date.now() } }) },
    { label: "釋放顯示卡記憶體", icon: "zap", disabled: !online, onClick: () => serverAction(server, "free") },
    { label: "中斷目前的工作", icon: "stop", disabled: !online, onClick: () => serverAction(server, "interrupt") },
    { label: "在瀏覽器開啟 ComfyUI", icon: "external", onClick: () => window.open(server.url, "_blank", "noopener") },
    { label: server.enabled === false ? "啟用" : "停用", icon: "power", onClick: onToggle },
    { label: "往前移", icon: "chevron-up", disabled: index === 0, onClick: () => onMove(-1) },
    { label: "往後移", icon: "chevron-down", disabled: index === count - 1, onClick: () => onMove(1) },
    { label: "刪除", icon: "trash", danger: true, onClick: onRemove },
  ]);
  return html`<article class=${`cs-card tone-${state.tone}`}>
    <header class="cs-head">
      <${Dot} tone=${state.tone} pulse=${online && st.queue && st.queue.running > 0} title=${state.label} />
      <div class="cs-title">
        <b>${server.name}</b>
        <span class="mono dim" title=${server.url}>${server.url}</span>
      </div>
      <${Badge} tone=${state.tone === "ok" ? "ok" : state.tone === "danger" ? "danger" : "neutral"}>${state.label}<//>
      ${index === 0 && html`<${Badge} tone="neutral" title="路由設成「自動」時，依順序選第一台在線且模型齊全的伺服器">優先<//>`}
      <span class="grow"></span>
      <${IconButton} icon="refresh" title="重新檢查" disabled=${busy || server.enabled === false} onClick=${recheck} />
      <${IconButton} icon="pencil" title="編輯" onClick=${() => openServerDialog(server)} />
      <${IconButton} icon="more" title="更多動作" onClick=${menu} />
    </header>
    ${online
      ? html`<div class="cs-body">
          <div class="kv">
            <div class="kv-row"><span>版本</span><b>${st.version ? `ComfyUI ${st.version}` : "未知"}</b></div>
            <div class="kv-row"><span>裝置</span><b>${dev ? dev.name : "沒有回報顯示卡"}</b></div>
            <div class="kv-row"><span>佇列</span><b>執行 ${(st.queue && st.queue.running) || 0}，等待 ${(st.queue && st.queue.pending) || 0}</b></div>
            <div class="kv-row"><span>回應時間</span><b>${st.latencyMs != null ? `${st.latencyMs} ms` : "未知"}</b></div>
          </div>
          <div class="cs-meters">
            ${dev && html`<${Meter} label="顯示卡記憶體" free=${dev.vramFree} total=${dev.vramTotal} />`}
            ${st.ram && html`<${Meter} label="系統記憶體" free=${st.ram.free} total=${st.ram.total} />`}
          </div>
          <div class="cs-tags">
            ${st.feed ? html`<${Badge} tone="ok" title="已連上即時進度通道，可以看到生成中的預覽">即時進度<//>`
              : html`<${Badge} tone="neutral" title="改用定時查詢進度">輪詢進度<//>`}
            ${st.hasToken && html`<${Badge} tone="neutral"><${Icon} name="key" size=${12} />使用權杖<//>`}
            ${st.pytorch && html`<span class="dim mono">PyTorch ${st.pytorch}</span>`}
          </div>
        </div>`
      : html`<div class="cs-body cs-offline">
          ${server.enabled === false
            ? html`<p class="dim">已停用，不會接到生成工作。</p>`
            : html`<p class=${st && st.checked ? "danger-text" : "dim"}>${(st && st.error) || "尚未檢查"}</p>
              <p class="dim">請確認 ComfyUI 已啟動、位址與連接埠正確；另一台電腦需以 --listen 啟動 ComfyUI。</p>`}
        </div>`}
    ${st && st.checked && html`<footer class="cs-foot dim">上次檢查：${fmtRelTime(st.checked)}</footer>`}
  </article>`;
}

function SharedOptions({ comfy, workflows }) {
  const upscale = workflows.filter((w) => w.kind === "upscale").map((w) => ({ value: w.id, label: w.name }));
  const save = (patch) => saveSettings({ comfy: patch });
  return html`<${Section} title="共用選項" icon="sliders" class="eng-section">
    <div class="row-2">
      <${Field} label="單一工作逾時" hint="超過這個時間還沒完成，就當作失敗並從 ComfyUI 佇列移除">
        <${NumberInput} value=${comfy.timeoutMin} min=${1} max=${240} step=${5} suffix="分鐘"
          onCommit=${(v) => v != null && save({ timeoutMin: Math.round(v) })} />
      <//>
      <${Field} label="放大使用的範本" hint="鏡頭檢視器的「放大」按鈕使用">
        <${Select} value=${comfy.upscaleWorkflow} options=${upscale} placeholder="選擇放大範本"
          onChange=${(v) => save({ upscaleWorkflow: v })} />
      <//>
    </div>
    <${Toggle} checked=${comfy.livePreview !== false} onChange=${(v) => save({ livePreview: v })}
      label="生成時顯示即時預覽" hint="ComfyUI 有開啟預覽時，佇列與鏡頭卡片會顯示生成中的畫面" />
    <${Toggle} checked=${!!comfy.useComfyOrgKey} onChange=${(v) => save({ useComfyOrgKey: v })}
      label="使用 Comfy 官方 API 節點" hint="工作流程含 Comfy.org 付費 API 節點（例如 Kling、Veo 節點）時，隨工作送出下面的金鑰" />
    ${comfy.useComfyOrgKey && html`<${SecretField} name="comfyorg.api_key" label="Comfy.org API 金鑰"
      help="只在送出工作時附加給 ComfyUI，不會寫進工作紀錄。" />`}
  <//>`;
}

export function ComfyTab() {
  const { settings, comfy, workflows } = useStore((s) => ({ settings: s.settings, comfy: s.comfy, workflows: s.workflows }));
  const [checking, setChecking] = useState(false);
  const cfg = settings.comfy;
  const servers = cfg.servers || NONE;
  const byId = useMemo(() => new Map((comfy || NONE).map((c) => [c.id, c])), [comfy]);

  const checkAll = async () => {
    setChecking(true);
    try {
      setState({ comfy: await api.post("/api/comfy/check") });
    } catch (err) {
      toastError(err, "無法檢查伺服器");
    } finally {
      setChecking(false);
    }
  };
  const writeServers = (next) => saveSettings({ comfy: { servers: next } });
  const move = (i) => (dir) => {
    const next = servers.slice();
    const [s] = next.splice(i, 1);
    next.splice(i + dir, 0, s);
    writeServers(next);
  };
  const toggle = (i) => () => writeServers(servers.map((s, j) => (j === i ? { ...s, enabled: s.enabled === false } : s)));
  const remove = (i) => async () => {
    const s = servers[i];
    const ok = await confirmDialog({ title: `刪除「${s.name}」`, danger: true, confirmLabel: "刪除",
      message: "伺服器的存取權杖會一併清除。指定這台伺服器的路由會找不到伺服器，請改成「自動」或其他伺服器。" });
    if (ok && (await writeServers(servers.filter((_, j) => j !== i)))) toast(`已刪除「${s.name}」`);
  };

  return html`<div class="eng-pane">
    <div class="eng-toolbar">
      <p class="eng-intro">程式透過 ComfyUI 的 API 送出工作流程，可以同時登錄本機與其他電腦（區網或 Tailscale）的 ComfyUI。
        路由選「自動」時，依下面的順序挑第一台在線、且範本需要的節點與模型都齊全的伺服器。</p>
      <span class="grow"></span>
      <${Button} icon="refresh" busy=${checking} onClick=${checkAll}>全部重新檢查<//>
      <${Button} kind="primary" icon="plus" onClick=${() => openServerDialog(null)}>新增伺服器<//>
    </div>
    ${servers.length
      ? html`<div class="cs-grid">
          ${servers.map((s, i) => html`<${ServerCard} key=${s.id} server=${s} st=${byId.get(s.id)} index=${i}
            count=${servers.length} onMove=${move(i)} onToggle=${toggle(i)} onRemove=${remove(i)} />`)}
        </div>`
      : html`<${Empty} icon="server" title="還沒有 ComfyUI 伺服器"
          actions=${html`<${Button} kind="primary" icon="plus" onClick=${() => openServerDialog(null)}>新增伺服器<//>`}>
          新增本機或其他電腦上的 ComfyUI 後，就能用範本生成圖片與影片。
        <//>`}
    <${SharedOptions} comfy=${cfg} workflows=${workflows || NONE} />
  </div>`;
}
