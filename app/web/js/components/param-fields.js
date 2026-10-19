// Inputs for the parameters a ComfyUI template declares (steps, CFG, sampler...). A value set here
// overrides the template default; the undo button beside it drops the override again.
import { html } from "../lib/h.js";
import { Field, NumberInput, Select, Toggle, DraftText, IconButton } from "./ui.js";

const isSet = (v) => v != null && v !== "";

// Decimal places a float input shows: enough for its step, at least two.
function digitsFor(step) {
  const frac = String(step ?? "").split(".")[1] || "";
  return Math.max(2, frac.length);
}

function defaultText(p) {
  if (p.type === "bool") return p.default ? "開" : "關";
  if (p.type === "select") {
    const opt = (p.options || []).find((o) => o.value === p.default);
    return opt ? opt.label : String(p.default ?? "");
  }
  return isSet(p.default) ? String(p.default) : "空白";
}

function ParamInput({ p, value, set, reset }) {
  if (p.type === "int" || p.type === "float") {
    const int = p.type === "int";
    return html`<${NumberInput} value=${value} min=${p.min ?? null} max=${p.max ?? null} nullable
      step=${p.step ?? (int ? 1 : 0.1)} digits=${int ? 0 : digitsFor(p.step)}
      placeholder=${defaultText(p)} onCommit=${(v) => (v == null ? reset() : set(int ? Math.round(v) : v))} />`;
  }
  if (p.type === "select") return html`<${Select} value=${value} options=${p.options || []} onChange=${set} />`;
  if (p.type === "bool") return html`<${Toggle} checked=${!!value} label=${p.label} onChange=${set} />`;
  // Multiline strings (e.g. a JSON element list) start from the default text so it can be edited in place.
  if (p.multiline) {
    return html`<${DraftText} multiline rows=${6} mono value=${isSet(value) ? String(value) : String(p.default ?? "")}
      maxLength=${4000} onCommit=${(v) => (v.trim() ? set(v) : reset())} />`;
  }
  return html`<${DraftText} value=${isSet(value) ? String(value) : ""} maxLength=${4000} placeholder=${defaultText(p)}
    onCommit=${(v) => (v.trim() ? set(v) : reset())} />`;
}

// params: what the template declares; values: the overrides saved so far. onChange receives the whole new dict.
export function ParamFields({ params, values, onChange }) {
  if (!params || !params.length) return null;
  const own = values || {};
  const set = (key) => (v) => onChange({ ...own, [key]: v });
  const reset = (key) => () => {
    if (key in own) onChange(Object.fromEntries(Object.entries(own).filter(([k]) => k !== key)));
  };
  return html`<div class="params">
    ${params.map((p) => {
      const overridden = isSet(own[p.key]);
      const undo = overridden && html`<${IconButton} icon="undo" size=${14} onClick=${reset(p.key)}
        title=${`還原預設值（${defaultText(p)}）`} />`;
      return html`<${Field} key=${p.key} inline=${!p.multiline} label=${p.type === "bool" ? "" : p.label} hint=${p.help || ""}
          after=${undo} class=${overridden ? "param-set" : ""}>
        <${ParamInput} p=${p} value=${overridden ? own[p.key] : p.default} set=${set(p.key)} reset=${reset(p.key)} />
      <//>`;
    })}
  </div>`;
}
