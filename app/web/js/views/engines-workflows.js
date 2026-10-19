// Engines page, workflow templates tab: built-in and imported ComfyUI templates, which routes use them,
// and a compatibility check against one server (missing custom nodes and model files, with download links).
import { html, useState, useEffect, useMemo, useRef } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { fmtBytes, fmtRelTime } from "../lib/format.js";
import { Button, IconButton, Chip, Select, Badge, Empty } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { openMenu, confirmDialog } from "../components/overlay.js";
import { toast, toastError } from "../components/toast.js";
import { openWorkflowEditor } from "../dialogs/workflow-editor.js";
import { KIND_TEXT, MODE_TEXT, BINDING_TEXT } from "../lib/workflow-labels.js";

const NONE = [];
const enc = encodeURIComponent;
const KINDS = [
  { id: "all", label: "全部" },
  { id: "image", label: "圖片", icon: "image" },
  { id: "video", label: "影片", icon: "film" },
  { id: "upscale", label: "放大", icon: "zoom-in" },
];
const KIND_ICON = { image: "image", video: "film", upscale: "zoom-in" };

// Which settings point at each template: route image/video engines and the upscale default.
function usageMap(settings) {
  const map = new Map();
  const add = (wid, label) => wid && map.set(wid, [...(map.get(wid) || NONE), label]);
  for (const r of settings.routes || NONE) {
    for (const kind of ["image", "video"]) {
      const sec = r[kind] || {};
      if (sec.backend !== "comfy") continue;
      add(sec.workflow, `路由「${r.name}」${KIND_TEXT[kind]}`);
      if (sec.workflowI2I && sec.workflowI2I !== sec.workflow) add(sec.workflowI2I, `路由「${r.name}」參考圖`);
    }
  }
  add(settings.comfy && settings.comfy.upscaleWorkflow, "預設放大範本");
  return map;
}

function downloadJson(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportTemplate(wf) {
  try {
    downloadJson(`${wf.id}.json`, await api.get(`/api/workflows/${enc(wf.id)}`));
  } catch (err) {
    toastError(err, "無法匯出範本");
  }
}

async function duplicateTemplate(wf) {
  try {
    const copy = await api.post(`/api/workflows/${enc(wf.id)}/duplicate`);
    toast(`已複製成「${copy.name}」`, { level: "success" });
    return copy;
  } catch (err) {
    toastError(err, "無法複製範本");
    return null;
  }
}

async function deleteTemplate(wf, users) {
  if (users.length) {
    toast(`${users.join("、")}正在使用「${wf.name}」，請先改用其他範本再刪除`, { level: "warn", duration: 6000 });
    return;
  }
  const ok = await confirmDialog({ title: `刪除範本「${wf.name}」`, danger: true, confirmLabel: "刪除",
    message: "範本檔會從資料夾移除，已經生成的圖片與影片不受影響。指定這個範本的鏡頭會在送出時提示找不到範本。" });
  if (!ok) return;
  try {
    await api.del(`/api/workflows/${enc(wf.id)}`);
    toast(`已刪除「${wf.name}」`);
  } catch (err) {
    toastError(err, "無法刪除範本");
  }
}

function CheckResult({ report, wf, serverName }) {
  const [open, setOpen] = useState(false);
  if (!report) return null;
  if (report.ok) {
    return html`<div class="wf-check ok"><${Icon} name="check-circle" size=${14} />「${serverName}」的節點與模型都齊全</div>`;
  }
  const sizes = new Map((wf.download || NONE).map((d) => [d.file, d.size]));
  const nodes = report.missingNodes || NONE;
  const models = report.missingModels || NONE;
  const parts = [nodes.length && `${nodes.length} 個節點`, models.length && `${models.length} 個模型檔`].filter(Boolean);
  return html`<div class="wf-check warn">
    <button type="button" class="wf-check-toggle" aria-expanded=${open ? "true" : "false"} onClick=${() => setOpen(!open)}>
      <${Icon} name="alert" size=${14} />「${serverName}」缺少 ${parts.join("、")}
      <${Icon} name=${open ? "chevron-up" : "chevron-down"} size=${14} />
    </button>
    ${open && html`<ul class="wf-missing">
      ${nodes.map((n) => html`<li key=${`n-${n}`}>
        <${Badge} tone="danger">節點<//><span class="mono">${n}</span>
        <span class="dim">需安裝提供這個節點的自訂節點套件（可用 ComfyUI Manager 搜尋）</span>
      </li>`)}
      ${models.map((m) => html`<li key=${`m-${m.node}-${m.input}`}>
        <${Badge} tone="warn">模型<//><span class="mono">${m.value}</span>
        <span class="dim">${m.folder ? `放到 models/${m.folder}` : `節點 ${m.class} 的 ${m.input}`}${sizes.get(m.value) ? `，約 ${fmtBytes(sizes.get(m.value))}` : ""}</span>
        ${m.url && html`<a class="wf-link" href=${m.url} target="_blank" rel="noopener noreferrer" title=${m.url}>
          <${Icon} name="external" size=${13} />下載頁</a>`}
      </li>`)}
    </ul>`}
  </div>`;
}

function TemplateCard({ wf, users, report, serverName }) {
  const keys = Object.keys(wf.bindings || {});
  const core = keys.filter((k) => BINDING_TEXT[k]);
  const menu = (e) => openMenu(e, [
    { label: wf.builtin ? "檢視內容" : "編輯", icon: wf.builtin ? "eye" : "pencil", onClick: () => openWorkflowEditor(wf.id) },
    { label: "複製一份", icon: "copy", onClick: () => duplicateTemplate(wf) },
    { label: "匯出 JSON", icon: "download", onClick: () => exportTemplate(wf) },
    !wf.builtin && { divider: true },
    !wf.builtin && { label: "刪除", icon: "trash", danger: true, onClick: () => deleteTemplate(wf, users) },
  ]);
  return html`<article class=${`wf-card kind-${wf.kind}`}>
    <header class="wf-head">
      <span class="wf-kind"><${Icon} name=${KIND_ICON[wf.kind] || "workflow"} size=${16} /></span>
      <div class="wf-title">
        <b>${wf.name}</b>
        <span class="mono dim">${wf.id}</span>
      </div>
      <span class="grow"></span>
      <${IconButton} icon=${wf.builtin ? "eye" : "pencil"} title=${wf.builtin ? "檢視內容" : "編輯"} onClick=${() => openWorkflowEditor(wf.id)} />
      <${IconButton} icon="more" title="更多動作" onClick=${menu} />
    </header>
    <div class="wf-badges">
      <${Badge} tone="blue">${MODE_TEXT[wf.mode] || wf.mode}<//>
      ${wf.builtin ? html`<${Badge} tone="neutral">內建<//>` : html`<${Badge} tone="accent">自訂<//>`}
      ${wf.verified
        ? html`<${Badge} tone="ok" title="範本標記為已在實機 ComfyUI 跑過">已實測<//>`
        : html`<${Badge} tone="warn" title="範本還沒有在實機跑過，參數取自官方範例或匯入的流程">未實測<//>`}
      ${wf.kind === "video" && wf.frameRule !== "any" && html`<${Badge} tone="neutral" title="影格數會自動調整成符合模型的數量">影格 ${wf.frameRule}<//>`}
      ${wf.kind === "video" && wf.maxFrames > 0 && html`<${Badge} tone="neutral">最多 ${wf.maxFrames} 格<//>`}
      ${wf.kind === "video" && wf.maxShortSide > 0 && html`<${Badge} tone="neutral" title="專案尺寸較大時會等比例縮小再生成">${wf.maxShortSide}P<//>`}
    </div>
    ${wf.description && html`<p class="wf-desc">${wf.description}</p>`}
    <div class="wf-meta">
      <span title="程式會自動填入這些節點輸入"><${Icon} name="link" size=${13} />${core.length ? core.map((k) => BINDING_TEXT[k]).join("、") : "沒有綁定"}</span>
      ${wf.params && wf.params.length > 0 && html`<span title="可在鏡頭檢視器調整的參數"><${Icon} name="sliders" size=${13} />${wf.params.map((p) => p.label).join("、")}</span>`}
    </div>
    ${!keys.includes("prompt") && (wf.kind === "image" || wf.mode === "t2v") && html`<p class="warn-text wf-warn">
      <${Icon} name="alert" size=${13} />沒有綁定提示詞，鏡頭的提示詞不會送進這個範本</p>`}
    <${CheckResult} report=${report} wf=${wf} serverName=${serverName} />
    <footer class="wf-foot dim">
      ${users.length ? html`<span class="wf-used"><${Icon} name="route" size=${13} />${users.join("、")}</span>` : html`<span>沒有路由使用</span>`}
      <span class="grow"></span>
      ${!wf.builtin && wf.updated && html`<span>更新於 ${fmtRelTime(wf.updated)}</span>`}
    </footer>
  </article>`;
}

export function WorkflowsTab() {
  const { settings, workflows, comfy, wfCheck } = useStore((s) => ({
    settings: s.settings, workflows: s.workflows, comfy: s.comfy, wfCheck: s.wfCheck,
  }));
  const [kind, setKind] = useState("all");
  const [query, setQuery] = useState("");
  const [sid, setSid] = useState("");
  const [check, setCheck] = useState({ sid: "", report: null, at: 0, busy: false });
  const [importing, setImporting] = useState(false);
  const fileRef = useRef(null);
  const servers = (settings.comfy && settings.comfy.servers) || NONE;
  const status = useMemo(() => new Map((comfy || NONE).map((c) => [c.id, c])), [comfy]);
  const usage = useMemo(() => usageMap(settings), [settings]);
  const enabled = servers.filter((s) => s.enabled !== false);
  const fallbackSid = (enabled.find((s) => status.get(s.id) && status.get(s.id).online) || enabled[0] || {}).id || "";
  const target = sid && servers.some((s) => s.id === sid) ? sid : fallbackSid;
  const targetServer = servers.find((s) => s.id === target);

  const runCheck = async (id) => {
    if (!id) return;
    setCheck({ sid: id, report: null, at: 0, busy: true });
    try {
      const report = await api.get(`/api/comfy/${enc(id)}/templates`);
      setCheck({ sid: id, report, at: Date.now(), busy: false });
    } catch (err) {
      setCheck({ sid: "", report: null, at: 0, busy: false });
      toastError(err, "無法檢查範本相容性");
    }
  };
  // The ComfyUI tab asks for a check on one server through the store.
  useEffect(() => {
    if (!wfCheck) return;
    setSid(wfCheck.sid);
    setState({ wfCheck: null });
    runCheck(wfCheck.sid);
  }, [wfCheck]);

  const importFile = async (file) => {
    if (!file) return;
    setImporting(true);
    try {
      let data;
      try {
        data = JSON.parse(await file.text());
      } catch {
        throw new Error("檔案不是有效的 JSON，請匯入 ComfyUI 存下的工作流程檔");
      }
      const st = target && status.get(target);
      const res = await api.post("/api/workflows/import", {
        data, name: file.name, server: st && st.online ? target : "",
      });
      toast(`已匯入「${res.workflow.name}」，請確認類型與綁定`, { level: "success" });
      if (res.warnings && res.warnings.length) {
        toast(res.warnings.join("；"), { level: "warn", title: "匯入提醒", duration: 9000 });
      }
      openWorkflowEditor(res.workflow.id);
    } catch (err) {
      toastError(err, "無法匯入工作流程");
    } finally {
      setImporting(false);
    }
  };

  const needle = query.trim().toLowerCase();
  const list = (workflows || NONE).filter((w) => (kind === "all" || w.kind === kind)
    && (!needle || `${w.name} ${w.id} ${w.description}`.toLowerCase().includes(needle)));
  const counts = Object.fromEntries(KINDS.map((k) => [k.id, (workflows || NONE).filter((w) => k.id === "all" || w.kind === k.id).length]));
  const reportFor = (wid) => (check.report && check.sid === target ? check.report[wid] : null);
  const okCount = check.report && check.sid === target ? Object.values(check.report).filter((r) => r.ok).length : 0;

  return html`<div class="eng-pane">
    <div class="eng-toolbar">
      <p class="eng-intro">範本是存好的 ComfyUI 工作流程，加上「哪個節點填提示詞、種子、尺寸」的綁定。
        在 ComfyUI 調好的流程可以直接匯入（介面格式或 API 格式都行），程式會自動找出提示詞與尺寸節點。</p>
      <span class="grow"></span>
      <input ref=${fileRef} type="file" accept=".json,application/json" hidden
        onChange=${(e) => { const f = e.currentTarget.files[0]; e.currentTarget.value = ""; importFile(f); }} />
      <${Button} kind="primary" icon="upload" busy=${importing} onClick=${() => fileRef.current && fileRef.current.click()}>匯入工作流程<//>
    </div>
    <div class="wf-toolbar">
      <div class="chips">
        ${KINDS.map((k) => html`<${Chip} key=${k.id} icon=${k.icon} active=${kind === k.id} onClick=${() => setKind(k.id)}>
          ${k.label}<span class="chip-count">${counts[k.id]}</span><//>`)}
      </div>
      <label class="search-box">
        <${Icon} name="search" size=${15} />
        <input type="search" placeholder="搜尋範本" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
      </label>
      <span class="grow"></span>
      ${servers.length > 0 && html`<div class="wf-checkbar">
        <${Select} value=${target} onChange=${setSid} title="要檢查的 ComfyUI 伺服器"
          options=${servers.map((s) => ({ value: s.id, label: `${s.name}${status.get(s.id) && status.get(s.id).online ? "" : "（離線）"}` }))} />
        <${Button} icon="scan" busy=${check.busy} disabled=${!target} onClick=${() => runCheck(target)}>檢查相容性<//>
      </div>`}
    </div>
    ${check.report && check.sid === target && html`<div class=${`note ${okCount === Object.keys(check.report).length ? "note-ok" : "note-info"}`}>
      <${Icon} name="scan" size=${15} />
      <span class="note-text">「${targetServer ? targetServer.name : target}」可以直接執行 ${okCount} / ${Object.keys(check.report).length} 個範本。
        缺少的模型請依提示下載到 ComfyUI 的 models 資料夾，放好後在 ComfyUI 重新整理或重啟，再檢查一次。</span>
    </div>`}
    ${list.length
      ? html`<div class="wf-grid">
          ${list.map((w) => html`<${TemplateCard} key=${w.id} wf=${w} users=${usage.get(w.id) || NONE}
            report=${reportFor(w.id)} serverName=${targetServer ? targetServer.name : target} />`)}
        </div>`
      : html`<${Empty} icon="workflow" title=${(workflows || NONE).length ? "沒有符合的範本" : "還沒有範本"}
          actions=${needle || kind !== "all" ? html`<${Button} onClick=${() => { setKind("all"); setQuery(""); }}>清除篩選<//>` : null}>
          ${(workflows || NONE).length ? "" : "內建範本放在 app/workflows，匯入的範本存在資料夾 workflows。"}
        <//>`}
  </div>`;
}
