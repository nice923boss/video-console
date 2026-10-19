// Binding and node views for the workflow template editor. A binding maps one key (prompt, seed, a
// parameter key, ...) to one or more node inputs that the app overwrites before queueing the graph.
import { html, useState } from "../lib/h.js";
import { Button, IconButton, Select, TextInput, Badge } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { BINDING_TEXT } from "../lib/workflow-labels.js";

const NONE = [];
const KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,31}$/;
const CORE_KEYS = Object.keys(BINDING_TEXT);
const CUSTOM = "__custom__";

// ComfyUI API format: a link to another node's output is [nodeId, outputIndex].
export const isLink = (v) => Array.isArray(v) && v.length === 2 && (typeof v[0] === "string" || typeof v[0] === "number")
  && typeof v[1] === "number";

const byNodeId = (a, b) => (Number(a) - Number(b)) || String(a).localeCompare(String(b));

export function nodeTitle(graph, nid) {
  const node = graph[nid];
  if (!node) return `#${nid}（不存在）`;
  const title = node._meta && node._meta.title;
  return `#${nid} ${title && title !== node.class_type ? `${title}（${node.class_type}）` : node.class_type}`;
}

export const nodeOptions = (graph) => Object.keys(graph || {}).sort(byNodeId)
  .map((nid) => ({ value: nid, label: nodeTitle(graph, nid) }));

const preview = (v) => {
  const text = typeof v === "string" ? v : JSON.stringify(v);
  return text.length > 60 ? `${text.slice(0, 60)}…` : text;
};

function TargetPicker({ graph, onPick, label, disabled }) {
  const [node, setNode] = useState("");
  const [input, setInput] = useState("");
  const inputs = node && graph[node]
    ? Object.entries(graph[node].inputs || {}).filter(([, v]) => !isLink(v)).map(([k, v]) => ({ value: k, label: `${k}＝${preview(v)}` }))
    : NONE;
  return html`<div class="wfe-picker">
    <${Select} value=${node} placeholder="選擇節點" options=${nodeOptions(graph)} onChange=${(v) => { setNode(v); setInput(""); }} />
    <${Select} value=${input} placeholder=${node && !inputs.length ? "沒有可填的輸入" : "選擇輸入"} options=${inputs}
      disabled=${!node || !inputs.length} onChange=${setInput} />
    <${Button} size="sm" icon="plus" disabled=${disabled || !node || !input}
      onClick=${() => { onPick({ node, input }); setInput(""); }}>${label}<//>
  </div>`;
}

function BindingRow({ name, targets, graph, hasParam, onChange, readOnly }) {
  const core = BINDING_TEXT[name];
  const add = (t) => !targets.some((x) => x.node === t.node && x.input === t.input) && onChange([...targets, t]);
  return html`<div class="wfe-binding">
    <div class="wfe-binding-key">
      <b>${core || name}</b>${core && html`<code>${name}</code>`}
      ${!core && !hasParam && html`<span class="warn-text" title="填值時找不到同名參數，節點會沿用原本的值">
        <${Icon} name="alert" size=${13} />沒有對應參數</span>`}
    </div>
    <div class="wfe-targets">
      ${targets.map((t, i) => {
        const value = graph[t.node] && graph[t.node].inputs ? graph[t.node].inputs[t.input] : undefined;
        return html`<span class="wfe-target" key=${`${t.node}.${t.input}`} title=${value === undefined ? "節點沒有這個輸入" : `目前的值：${preview(value)}`}>
          <span>${nodeTitle(graph, t.node)}</span><${Icon} name="chevron-right" size=${12} /><code>${t.input}</code>
          ${value === undefined && html`<${Badge} tone="warn">找不到<//>`}
          ${!readOnly && html`<button type="button" class="wfe-target-x" title="移除" onClick=${() => onChange(targets.filter((_, j) => j !== i))}>
            <${Icon} name="x" size=${12} /></button>`}
        </span>`;
      })}
    </div>
    ${!readOnly && html`<${TargetPicker} graph=${graph} onPick=${add} label="加入" />`}
  </div>`;
}

function NewBinding({ graph, used, onAdd }) {
  const free = CORE_KEYS.filter((k) => !used.includes(k));
  const [picked, setChoice] = useState(free[0] || CUSTOM);
  // a core key that just got bound drops out of the list, so fall back to the next free one
  const choice = picked !== CUSTOM && !free.includes(picked) ? (free[0] || CUSTOM) : picked;
  const [custom, setCustom] = useState("");
  const key = choice === CUSTOM ? custom.trim() : choice;
  const error = choice === CUSTOM && custom && (!KEY_RE.test(key) ? "以英文字母開頭，只能用英數字與底線"
    : used.includes(key) ? "這個名稱已經有綁定" : "");
  const options = [...free.map((k) => ({ value: k, label: `${BINDING_TEXT[k]}（${k}）` })), { value: CUSTOM, label: "自訂名稱（對應參數代號）" }];
  return html`<div class="wfe-new-binding">
    <h4>新增綁定</h4>
    <div class="wfe-picker">
      <${Select} value=${choice} options=${options} onChange=${setChoice} />
      ${choice === CUSTOM && html`<${TextInput} mono value=${custom} placeholder="例如 steps" maxLength=${32} onChange=${setCustom} />`}
    </div>
    ${error && html`<p class="field-error">${error}</p>`}
    <${TargetPicker} graph=${graph} label="新增綁定" disabled=${!key || !!error}
      onPick=${(t) => { onAdd(key, t); setCustom(""); }} />
  </div>`;
}

export function BindingsEditor({ graph, bindings, params, onChange, readOnly }) {
  const keys = Object.keys(bindings || {});
  const ordered = [...CORE_KEYS.filter((k) => keys.includes(k)), ...keys.filter((k) => !BINDING_TEXT[k]).sort()];
  const paramKeys = (params || NONE).map((p) => p.key);
  const setKey = (k) => (targets) => {
    const { [k]: _, ...rest } = bindings;
    onChange(targets.length ? { ...rest, [k]: targets } : rest);
  };
  return html`<div class="wfe-bindings">
    <p class="dim">程式在送出工作前，會把提示詞、種子、尺寸等值寫進這些節點輸入。自訂名稱要和「參數」分頁的代號一致才會套用。</p>
    ${ordered.map((k) => html`<${BindingRow} key=${k} name=${k} targets=${bindings[k]} graph=${graph}
      hasParam=${paramKeys.includes(k)} onChange=${setKey(k)} readOnly=${readOnly} />`)}
    ${!ordered.length && html`<p class="dim wfe-empty">還沒有任何綁定，這個範本只會照原樣送出。</p>`}
    ${!readOnly && html`<${NewBinding} graph=${graph} used=${keys}
      onAdd=${(k, t) => onChange({ ...bindings, [k]: [...(bindings[k] || NONE), t] })} />`}
  </div>`;
}

// Read-only listing of every node and its inputs; links show where the value comes from.
export function NodesList({ graph, bindings, outputs }) {
  const bound = new Map();
  for (const [k, targets] of Object.entries(bindings || {})) {
    for (const t of targets) bound.set(`${t.node}.${t.input}`, BINDING_TEXT[k] || k);
  }
  return html`<div class="wfe-nodes">
    ${Object.keys(graph || {}).sort(byNodeId).map((nid) => html`<details class="wfe-node" key=${nid}>
      <summary>
        <span>${nodeTitle(graph, nid)}</span>
        ${(outputs || NONE).includes(nid) && html`<${Badge} tone="accent">輸出<//>`}
      </summary>
      <div class="kv">
        ${Object.entries(graph[nid].inputs || {}).map(([k, v]) => html`<div class="kv-row" key=${k}>
          <span class="mono">${k}</span>
          <span>${isLink(v) ? html`<span class="dim">← ${nodeTitle(graph, String(v[0]))}</span>` : preview(v)}
            ${bound.has(`${nid}.${k}`) && html` <${Badge} tone="blue">${bound.get(`${nid}.${k}`)}<//>`}</span>
        </div>`)}
      </div>
    </details>`)}
  </div>`;
}
