// Workflow template editor: name, kind, output nodes, bindings and parameters. Built-in templates open
// read-only with a "duplicate to edit" action, because the backend refuses to overwrite them.
import { html, useState, useEffect } from "../lib/h.js";
import { api } from "../lib/api.js";
import { fmtBytes } from "../lib/format.js";
import { KIND_TEXT, MODE_TEXT, MODES_BY_KIND } from "../lib/workflow-labels.js";
import { openModal, Modal, confirmDialog } from "../components/overlay.js";
import { Button, Field, TextInput, TextArea, NumberInput, Select, Tabs, Chip, Badge, Spinner } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast, toastError } from "../components/toast.js";
import { BindingsEditor, NodesList, nodeTitle } from "./workflow-bindings.js";
import { ParamsEditor, paramErrors } from "./workflow-params.js";

const NONE = [];
const enc = encodeURIComponent;
const OUTPUT_CLASSES = new Set(["SaveImage", "PreviewImage", "SaveVideo", "SaveAnimatedWEBP", "SaveAnimatedPNG",
  "SaveWEBM", "VHS_VideoCombine", "Image Comparer (rgthree)"]);
const KIND_OPTIONS = Object.entries(KIND_TEXT).map(([value, label]) => ({ value, label }));
const FRAME_RULES = [
  { value: "any", label: "不限" },
  { value: "4n+1", label: "4n+1（Wan、Hunyuan）" },
  { value: "8n+1", label: "8n+1（LTX-Video）" },
];
const NEEDS_IMAGE = new Set(["i2i", "i2v", "upscale"]);

const normalize = (t) => ({ ...t, bindings: t.bindings || {}, params: t.params || NONE, outputs: t.outputs || NONE,
  download: t.download || NONE, notes: t.notes || "", description: t.description || "" });

function Problems({ draft }) {
  const b = draft.bindings;
  const list = [
    !b.prompt && (draft.kind === "image" || draft.mode === "t2v") && "沒有綁定提示詞，鏡頭的提示詞不會送進這個流程。",
    !b.image && NEEDS_IMAGE.has(draft.mode) && "沒有綁定輸入圖片，參考圖或起始畫面不會送進這個流程。",
    draft.kind === "video" && !b.frames && "沒有綁定影格數，影片長度會固定為流程原本的設定。",
  ].filter(Boolean);
  return list.map((text) => html`<div class="note note-warn" key=${text}>
    <${Icon} name="alert" size=${15} /><span class="note-text">${text}</span>
  </div>`);
}

function OutputPicker({ draft, set, readOnly }) {
  const ids = Object.keys(draft.graph);
  const preferred = ids.filter((nid) => OUTPUT_CLASSES.has(draft.graph[nid].class_type) || draft.outputs.includes(nid));
  const candidates = preferred.length ? preferred : ids;
  const toggle = (nid) => set({ outputs: draft.outputs.includes(nid) ? draft.outputs.filter((x) => x !== nid) : [...draft.outputs, nid] });
  return html`<${Field} label="輸出節點" hint="沒有指定時，會收集所有節點產生的檔案；指定後只取這些節點的輸出">
    <div class="chips wfe-outputs">
      ${candidates.map((nid) => html`<${Chip} key=${nid} active=${draft.outputs.includes(nid)} disabled=${readOnly}
        icon=${draft.outputs.includes(nid) ? "check" : "dot"} onClick=${() => toggle(nid)}>${nodeTitle(draft.graph, nid)}<//>`)}
    </div>
  <//>`;
}

function Downloads({ items }) {
  if (!items.length) return null;
  return html`<${Field} label="需要的模型檔" hint="放進 ComfyUI 的 models 底下對應資料夾；相容性檢查會列出缺少的檔案">
    <ul class="wfe-downloads">
      ${items.map((d) => html`<li key=${d.file}>
        <code>${d.folder ? `${d.folder}/` : ""}${d.file}</code>
        ${d.size ? html`<span class="dim">${fmtBytes(d.size)}</span>` : null}
        ${d.url && html`<a class="wf-link" href=${d.url} target="_blank" rel="noopener noreferrer">
          <${Icon} name="external" size=${13} />下載頁面</a>`}
      </li>`)}
    </ul>
  <//>`;
}

function BasicTab({ draft, set, readOnly }) {
  const modes = (MODES_BY_KIND[draft.kind] || NONE).map((m) => ({ value: m, label: MODE_TEXT[m] }));
  const setKind = (kind) => set({ kind, mode: MODES_BY_KIND[kind].includes(draft.mode) ? draft.mode : MODES_BY_KIND[kind][0] });
  return html`<div class="wfe-basic">
    <${Problems} draft=${draft} />
    <div class="row-2">
      <${Field} label="名稱" error=${draft.name.trim() ? "" : "請填寫名稱"}>
        <${TextInput} value=${draft.name} maxLength=${80} disabled=${readOnly} onChange=${(name) => set({ name })} />
      <//>
      <${Field} label="代號" hint="建立時決定，之後不能更改">
        <${TextInput} mono value=${draft.id} disabled />
      <//>
    </div>
    <${Field} label="說明">
      <${TextArea} value=${draft.description} rows=${2} autosize maxLength=${400} disabled=${readOnly}
        placeholder="這個流程適合做什麼、需要哪些模型" onChange=${(description) => set({ description })} />
    <//>
    <div class="row-2">
      <${Field} label="類型"><${Select} value=${draft.kind} options=${KIND_OPTIONS} disabled=${readOnly} onChange=${setKind} /><//>
      <${Field} label="用途"><${Select} value=${draft.mode} options=${modes} disabled=${readOnly} onChange=${(mode) => set({ mode })} /><//>
    </div>
    ${draft.kind === "video" && html`<div class="row-3">
      <${Field} label="影格數規則" hint="模型要求的影格數格式，送出前會自動調整">
        <${Select} value=${draft.frameRule} options=${FRAME_RULES} disabled=${readOnly} onChange=${(frameRule) => set({ frameRule })} />
      <//>
      <${Field} label="最多影格數" hint="空白表示不限制">
        <${NumberInput} value=${draft.maxFrames || null} nullable min=${1} max=${2000} disabled=${readOnly} placeholder="不限"
          onCommit=${(v) => set({ maxFrames: v ? Math.round(v) : 0 })} />
      <//>
      <${Field} label="建議影格率" hint="空白表示沿用專案設定">
        <${NumberInput} value=${draft.defaultFps || null} nullable min=${1} max=${120} suffix="fps" disabled=${readOnly}
          placeholder="專案設定" onCommit=${(v) => set({ defaultFps: v ? Math.round(v) : 0 })} />
      <//>
    </div>
    <${Field} label="短邊上限" hint="專案尺寸的短邊超過這個值時，等比例縮小後再送進流程，例如 480 表示 480P；空白表示不限">
      <${NumberInput} value=${draft.maxShortSide || null} nullable min=${64} max=${4096} suffix="px" disabled=${readOnly}
        placeholder="不限" onCommit=${(v) => set({ maxShortSide: v ? Math.round(v) : 0 })} />
    <//>`}
    <${OutputPicker} draft=${draft} set=${set} readOnly=${readOnly} />
    <${Downloads} items=${draft.download} />
    <${Field} label="備註">
      <${TextArea} value=${draft.notes} rows=${3} autosize maxLength=${2000} disabled=${readOnly}
        placeholder="例如模型下載位置、建議的步數或已知問題" onChange=${(notes) => set({ notes })} />
    <//>
  </div>`;
}

function EditorDialog({ wid, close }) {
  const [base, setBase] = useState(null);
  const [draft, setDraft] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [tab, setTab] = useState("basic");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    api.get(`/api/workflows/${enc(wid)}`)
      .then((tpl) => {
        if (!alive) return;
        const d = normalize(tpl);
        setBase(d);
        setDraft(d);
      })
      .catch((err) => alive && setLoadError(err.message));
    return () => { alive = false; };
  }, [wid]);

  const readOnly = !draft || draft.builtin;
  const dirty = !!draft && JSON.stringify(draft) !== JSON.stringify(base);
  const invalid = !draft || !draft.name.trim() || paramErrors(draft.params);
  const set = (patch) => setDraft((d) => ({ ...d, ...patch }));

  const tryClose = async () => {
    if (dirty && !(await confirmDialog({ title: "放棄變更？", message: "範本還有沒儲存的修改，關閉後會遺失。",
      confirmLabel: "放棄變更", danger: true }))) return;
    close(null);
  };

  const save = async () => {
    setSaving(true);
    try {
      const saved = await api.put(`/api/workflows/${enc(draft.id)}`, { ...draft, name: draft.name.trim() });
      toast(`已儲存範本「${saved.name}」`, { level: "success" });
      close(saved);
    } catch (err) {
      toastError(err, "範本沒有儲存");
      setSaving(false);
    }
  };

  const duplicate = async () => {
    try {
      const copy = await api.post(`/api/workflows/${enc(wid)}/duplicate`);
      toast(`已複製為「${copy.name}」，可以開始編輯`, { level: "success" });
      close(null);
      openWorkflowEditor(copy.id);
    } catch (err) {
      toastError(err, "無法複製範本");
    }
  };

  const unbound = draft ? draft.params.filter((p) => !draft.bindings[p.key]).length : 0;
  const tabs = [
    { id: "basic", label: "基本", icon: "info" },
    { id: "bindings", label: "綁定", icon: "link", badge: draft ? Object.keys(draft.bindings).length : null },
    { id: "params", label: "參數", icon: "sliders", badge: draft ? draft.params.length : null,
      title: unbound ? `${unbound} 個參數沒有同名綁定` : undefined },
    { id: "nodes", label: "節點", icon: "workflow", badge: draft ? Object.keys(draft.graph).length : null },
  ];

  const footer = !draft ? html`<span class="grow"></span><${Button} onClick=${() => close(null)}>關閉<//>`
    : readOnly ? html`
      <span class="modal-foot-note"><${Icon} name="lock" size=${14} />內建範本只能檢視，複製一份後就能修改</span>
      <span class="grow"></span>
      <${Button} onClick=${() => close(null)}>關閉<//>
      <${Button} kind="primary" icon="copy" onClick=${duplicate}>複製後編輯<//>`
    : html`
      ${dirty && html`<span class="eng-dirty">有未儲存的修改</span>`}
      <span class="grow"></span>
      <${Button} onClick=${tryClose}>取消<//>
      <${Button} kind="primary" icon="save" busy=${saving} disabled=${!dirty || invalid} onClick=${save}>儲存<//>`;

  const title = draft ? `${readOnly ? "檢視" : "編輯"}範本「${draft.name || draft.id}」` : "工作流程範本";
  return html`<${Modal} title=${title} width=${820} backdropClose=${false} onClose=${tryClose} footer=${footer} class="wf-editor">
    ${!draft && !loadError && html`<div class="exp-loading"><${Spinner} /><span>正在讀取範本…</span></div>`}
    ${loadError && html`<div class="note note-danger"><${Icon} name="x-circle" size=${15} />
      <span class="note-text"><b>無法讀取範本</b>：${loadError}</span></div>`}
    ${draft && html`
      <div class="wfe-head">
        <${Badge} tone="blue">${KIND_TEXT[draft.kind]}・${MODE_TEXT[draft.mode] || draft.mode}<//>
        ${draft.builtin ? html`<${Badge} tone="neutral">內建<//>` : html`<${Badge} tone="accent">自訂<//>`}
        ${draft.verified ? html`<${Badge} tone="ok">已實測<//>` : html`<${Badge} tone="warn">未實測<//>`}
      </div>
      <${Tabs} value=${tab} onChange=${setTab} tabs=${tabs} class="wfe-tabs" />
      <div class="wfe-body">
        ${tab === "basic" && html`<${BasicTab} draft=${draft} set=${set} readOnly=${readOnly} />`}
        ${tab === "bindings" && html`<${BindingsEditor} graph=${draft.graph} bindings=${draft.bindings} params=${draft.params}
          readOnly=${readOnly} onChange=${(bindings) => set({ bindings })} />`}
        ${tab === "params" && html`<${ParamsEditor} params=${draft.params} bindings=${draft.bindings} readOnly=${readOnly}
          onChange=${(params) => set({ params })} />`}
        ${tab === "nodes" && html`
          <p class="dim">節點內容來自匯入的流程。要增減節點，請在 ComfyUI 改好後重新匯入。</p>
          <${NodesList} graph=${draft.graph} bindings=${draft.bindings} outputs=${draft.outputs} />`}
      </div>`}
  <//>`;
}

// Resolves with the saved template, or null when the dialog closes without saving.
export function openWorkflowEditor(wid) {
  return openModal((close) => html`<${EditorDialog} wid=${wid} close=${close} />`);
}
