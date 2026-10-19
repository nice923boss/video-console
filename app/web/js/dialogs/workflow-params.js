// Parameter list editor for a workflow template: each parameter becomes an input in the shot inspector
// and is written into the graph through the binding with the same key.
import { html } from "../lib/h.js";
import { Button, IconButton, Field, TextInput, NumberInput, Select, Toggle } from "../components/ui.js";
import { ParamFields } from "../components/param-fields.js";
import { Icon } from "../components/icons.js";

const NONE = [];
const KEY_RE = /^[A-Za-z][A-Za-z0-9_]{0,31}$/;
const TYPES = [
  { value: "int", label: "整數" },
  { value: "float", label: "小數" },
  { value: "select", label: "選單" },
  { value: "bool", label: "開關" },
  { value: "string", label: "文字" },
];

// One option per line: "value" or "value = label". Numeric values stay numbers.
function parseOptions(text) {
  return text.split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 60).map((line) => {
    const [raw, ...rest] = line.split(" = ");
    const value = /^-?\d+(\.\d+)?$/.test(raw.trim()) ? Number(raw.trim()) : raw.trim();
    return { value, label: rest.join(" = ").trim() || String(value) };
  });
}

const optionsText = (opts) => (opts || NONE)
  .map((o) => (String(o.label) === String(o.value) ? String(o.value) : `${o.value} = ${o.label}`)).join("\n");

// Keep only the fields that make sense for the chosen type.
function retype(p, type) {
  const { min, max, step, options, ...rest } = p;
  const out = { ...rest, type };
  if (type === "int" || type === "float") Object.assign(out, { min, max, step });
  if (type === "select") out.options = options && options.length ? options : [{ value: "", label: "（空白）" }];
  if (type === "bool") out.default = !!p.default;
  else if (typeof p.default === "boolean") delete out.default;
  return out;
}

function DefaultInput({ p, onChange, disabled }) {
  const set = (v) => onChange({ ...p, default: v });
  if (p.type === "int" || p.type === "float") {
    return html`<${NumberInput} value=${p.default ?? null} nullable disabled=${disabled} step=${p.step ?? 1}
      onCommit=${(v) => (v == null ? onChange(Object.fromEntries(Object.entries(p).filter(([k]) => k !== "default"))) : set(p.type === "int" ? Math.round(v) : v))} />`;
  }
  if (p.type === "select") return html`<${Select} value=${p.default} options=${p.options || NONE} disabled=${disabled} onChange=${set} />`;
  if (p.type === "bool") return html`<${Toggle} checked=${!!p.default} disabled=${disabled} onChange=${set} />`;
  return html`<${TextInput} value=${p.default ?? ""} disabled=${disabled} onChange=${set} />`;
}

function ParamRow({ p, index, count, bound, keys, onChange, onRemove, onMove, readOnly }) {
  const keyError = !KEY_RE.test(p.key) ? "以英文字母開頭，只能用英數字與底線"
    : keys.filter((k) => k === p.key).length > 1 ? "代號重複" : "";
  const num = p.type === "int" || p.type === "float";
  const n = (k) => html`<${Field} label=${{ min: "最小值", max: "最大值", step: "間距" }[k]} inline>
    <${NumberInput} value=${p[k] ?? null} nullable disabled=${readOnly}
      onCommit=${(v) => onChange(v == null ? Object.fromEntries(Object.entries(p).filter(([x]) => x !== k)) : { ...p, [k]: v })} />
  <//>`;
  return html`<div class="wfe-param">
    <div class="wfe-param-head">
      <b>${p.label || p.key}</b>
      ${!bound && html`<span class="warn-text"><${Icon} name="alert" size=${13} />沒有同名綁定，調整不會生效</span>`}
      <span class="grow"></span>
      ${!readOnly && html`
        <${IconButton} icon="chevron-up" size=${15} title="往前移" disabled=${index === 0} onClick=${() => onMove(-1)} />
        <${IconButton} icon="chevron-down" size=${15} title="往後移" disabled=${index === count - 1} onClick=${() => onMove(1)} />
        <${IconButton} icon="trash" size=${15} title="移除參數" onClick=${onRemove} />`}
    </div>
    <div class="wfe-param-grid">
      <${Field} label="代號" error=${keyError} inline>
        <${TextInput} mono value=${p.key} maxLength=${32} disabled=${readOnly} onChange=${(v) => onChange({ ...p, key: v.trim() })} />
      <//>
      <${Field} label="顯示名稱" inline>
        <${TextInput} value=${p.label || ""} maxLength=${40} disabled=${readOnly} onChange=${(v) => onChange({ ...p, label: v })} />
      <//>
      <${Field} label="類型" inline>
        <${Select} value=${p.type} options=${TYPES} disabled=${readOnly} onChange=${(t) => onChange(retype(p, t))} />
      <//>
      <${Field} label="預設值" inline><${DefaultInput} p=${p} onChange=${onChange} disabled=${readOnly} /><//>
      ${num && html`${n("min")}${n("max")}${n("step")}`}
    </div>
    ${p.type === "select" && html`<${Field} label="選項" hint="一行一個；要顯示不同名稱時寫成「值 = 名稱」">
      <textarea class="input mono wfe-options" rows="4" disabled=${readOnly} value=${optionsText(p.options)}
        onChange=${(e) => onChange({ ...p, options: parseOptions(e.currentTarget.value) })}></textarea>
    <//>`}
    <${Field} label="說明" inline>
      <${TextInput} value=${p.help || ""} maxLength=${200} disabled=${readOnly} placeholder="顯示在鏡頭檢視器的提示文字"
        onChange=${(v) => onChange({ ...p, help: v })} />
    <//>
  </div>`;
}

export function paramErrors(params) {
  const keys = params.map((p) => p.key);
  return params.some((p, i) => !KEY_RE.test(p.key) || keys.indexOf(p.key) !== i);
}

// params: the template's list; bindings: {key: [targets]} so unbound parameters can be flagged.
export function ParamsEditor({ params, bindings, onChange, readOnly }) {
  const list = params || NONE;
  const keys = list.map((p) => p.key);
  const update = (i) => (p) => onChange(list.map((x, j) => (j === i ? p : x)));
  const move = (i) => (dir) => {
    const next = list.slice();
    const [p] = next.splice(i, 1);
    next.splice(i + dir, 0, p);
    onChange(next);
  };
  const add = () => {
    let n = list.length + 1;
    while (keys.includes(`param${n}`)) n += 1;
    onChange([...list, { key: `param${n}`, label: `參數 ${n}`, type: "float", min: 0, max: 1, step: 0.05, default: 0.5 }]);
  };
  const unboundKeys = Object.keys(bindings || {}).filter((k) => !keys.includes(k));
  return html`<div class="wfe-params">
    <p class="dim">參數會出現在鏡頭檢視器的「生成」分頁，填入的值透過同名綁定寫進節點；沒填就用預設值，預設值也空白時沿用節點原本的值。</p>
    ${list.map((p, i) => html`<${ParamRow} key=${i} p=${p} index=${i} count=${list.length} keys=${keys}
      bound=${!!(bindings && bindings[p.key])} readOnly=${readOnly}
      onChange=${update(i)} onRemove=${() => onChange(list.filter((_, j) => j !== i))} onMove=${move(i)} />`)}
    ${!list.length && html`<p class="dim wfe-empty">這個範本沒有可調整的參數。</p>`}
    ${!readOnly && html`<div class="wfe-row">
      <${Button} size="sm" icon="plus" onClick=${add}>新增參數<//>
      ${unboundKeys.length > 0 && html`<span class="dim">可直接對應的綁定：${unboundKeys.join("、")}</span>`}
    </div>`}
    ${list.length > 0 && html`<section class="wfe-preview">
      <h4>鏡頭檢視器預覽</h4>
      <${ParamFields} params=${list.filter((p) => KEY_RE.test(p.key))} values=${{}} onChange=${() => {}} />
    </section>`}
  </div>`;
}
