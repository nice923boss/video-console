// Small controls shared by every view: buttons, inputs, toggles, tabs and status bits.
import { html, useState, useEffect, useRef, useCallback, useLayoutEffect } from "../lib/h.js";
import { MOD_LABEL, comboParts } from "../lib/keys.js";
import { clamp, round } from "../lib/format.js";
import { Icon } from "./icons.js";

// IME state for the whole page: drafts wait for the composition to end before they save.
let composing = false;
document.addEventListener("compositionstart", () => { composing = true; }, true);
document.addEventListener("compositionend", () => { composing = false; }, true);

const cx = (...parts) => parts.filter(Boolean).join(" ");

export function Spinner({ size = 16 }) {
  return html`<span class="spinner" style=${`width:${size}px;height:${size}px`} aria-hidden="true"></span>`;
}

export function Button({ kind = "default", size = "md", icon = null, iconRight = null, busy = false, disabled = false,
  onClick, title, type = "button", class: cls = "", children, ...rest }) {
  const hasText = children != null && children !== false && children !== "";
  return html`<button type=${type} title=${title} disabled=${disabled || busy} onClick=${onClick} ...${rest}
      class=${cx("btn", kind !== "default" && `btn-${kind}`, size === "sm" && "btn-sm", !hasText && "btn-icon-only", busy && "is-busy", cls)}>
    ${busy ? html`<${Spinner} size=${14} />` : icon && html`<${Icon} name=${icon} size=${size === "sm" ? 14 : 16} />`}
    ${hasText && html`<span class="btn-text">${children}</span>`}
    ${iconRight && html`<${Icon} name=${iconRight} size=${14} />`}
  </button>`;
}

export function IconButton({ icon, title, onClick, active = false, disabled = false, size = 18, kind = "ghost", badge = null,
  class: cls = "", ...rest }) {
  return html`<button type="button" title=${title} aria-label=${title} disabled=${disabled} onClick=${onClick} ...${rest}
      class=${cx("icon-btn", kind !== "ghost" && `icon-btn-${kind}`, active && "active", cls)}>
    <${Icon} name=${icon} size=${size} />
    ${badge != null && badge !== 0 && badge !== "" && html`<span class="icon-badge">${badge}</span>`}
  </button>`;
}

export function Field({ label, hint, error, children, inline = false, class: cls = "", after = null }) {
  return html`<div class=${cx("field", inline && "field-inline", cls)}>
    ${(label || after) && html`<div class="field-top">
      ${label && html`<span class="field-label">${label}</span>`}
      ${after && html`<span class="field-after">${after}</span>`}
    </div>`}
    <div class="field-control">${children}</div>
    ${error ? html`<span class="field-error">${error}</span>` : hint && html`<span class="field-hint">${hint}</span>`}
  </div>`;
}

export function TextInput({ value, onChange, placeholder = "", type = "text", disabled = false, class: cls = "", mono = false, ...rest }) {
  return html`<input class=${cx("input", mono && "mono", cls)} type=${type} value=${value ?? ""} placeholder=${placeholder}
    disabled=${disabled} onInput=${(e) => onChange && onChange(e.currentTarget.value)} ...${rest} />`;
}

export function TextArea({ value, onChange, rows = 3, placeholder = "", autosize = false, class: cls = "", mono = false,
  disabled = false, ...rest }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!autosize || !el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }, [value, autosize]);
  return html`<textarea ref=${ref} class=${cx("input", "textarea", mono && "mono", autosize && "autosize", cls)} rows=${rows}
    value=${value ?? ""} placeholder=${placeholder} disabled=${disabled}
    onInput=${(e) => onChange && onChange(e.currentTarget.value)} ...${rest}></textarea>`;
}

// Text that saves itself: typing updates a local draft, which commits after a pause or on blur.
export function useDraft(value, commit, delay = 600) {
  const [draft, setDraft] = useState(value);
  const dirty = useRef(false);
  const timer = useRef(null);
  const latest = useRef({ draft: value, value, commit });
  latest.current.commit = commit;
  latest.current.value = value;
  useEffect(() => {
    if (dirty.current) return;
    latest.current.draft = value;
    setDraft(value);
  }, [value]);
  const flush = useCallback(() => {
    clearTimeout(timer.current);
    timer.current = null;
    if (!dirty.current) return;
    dirty.current = false;
    const cur = latest.current;
    if (cur.draft !== cur.value) cur.commit(cur.draft);
  }, []);
  const tick = useCallback(() => {
    if (composing) {
      timer.current = setTimeout(tick, delay);
      return;
    }
    flush();
  }, [delay, flush]);
  const change = useCallback((v) => {
    setDraft(v);
    dirty.current = true;
    latest.current.draft = v;
    clearTimeout(timer.current);
    timer.current = setTimeout(tick, delay);
  }, [delay, tick]);
  useEffect(() => () => flush(), []);
  return [draft, change, flush];
}

export function DraftText({ value, onCommit, multiline = false, rows = 3, autosize = true, delay = 600, ...rest }) {
  const [draft, change, flush] = useDraft(value ?? "", onCommit, delay);
  return multiline
    ? html`<${TextArea} value=${draft} onChange=${change} onBlur=${flush} rows=${rows} autosize=${autosize} ...${rest} />`
    : html`<${TextInput} value=${draft} onChange=${change} onBlur=${flush} ...${rest} />`;
}

// Numeric input that commits on Enter or blur; arrow keys step (Shift x10, Alt x0.1).
export function NumberInput({ value, onCommit, min = null, max = null, step = 1, digits = null, suffix = "",
  placeholder = "", disabled = false, nullable = false, class: cls = "", title }) {
  const fmt = (v) => (v == null || v === "" ? "" : String(digits != null ? round(Number(v), digits) : v));
  const [text, setText] = useState(fmt(value));
  const editing = useRef(false);
  useEffect(() => {
    if (!editing.current) setText(fmt(value));
  }, [value]);
  const parse = (t) => {
    const s = String(t).trim().replace(/,/g, "");
    if (!s) return nullable ? null : undefined;
    let v = Number(s);
    if (!Number.isFinite(v)) return undefined;
    if (min != null) v = Math.max(min, v);
    if (max != null) v = Math.min(max, v);
    return digits != null ? round(v, digits) : v;
  };
  const commit = () => {
    editing.current = false;
    const v = parse(text);
    if (v === undefined) {
      setText(fmt(value));
      return;
    }
    setText(fmt(v));
    if (v !== value) onCommit(v);
  };
  const bump = (dir, e) => {
    const cur = parse(text);
    const base = cur == null ? (min ?? 0) : cur;
    const mult = e.shiftKey ? 10 : e.altKey ? 0.1 : 1;
    const next = parse(String(round(base + dir * step * mult, 6)));
    if (next == null) return;
    editing.current = false;
    setText(fmt(next));
    if (next !== value) onCommit(next);
  };
  const onKeyDown = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape" && text !== fmt(value)) {
      e.stopPropagation();
      editing.current = false;
      setText(fmt(value));
    } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      bump(e.key === "ArrowUp" ? 1 : -1, e);
    }
  };
  return html`<div class=${cx("num", disabled && "is-disabled", cls)} title=${title}>
    <input class="input num-input" type="text" inputmode="decimal" value=${text} placeholder=${placeholder}
      disabled=${disabled} onFocus=${() => { editing.current = true; }} onBlur=${commit} onKeyDown=${onKeyDown}
      onInput=${(e) => { editing.current = true; setText(e.currentTarget.value); }} />
    ${suffix && html`<span class="num-suffix">${suffix}</span>`}
  </div>`;
}

// options: [{value, label, disabled}] or plain values. Values keep their type (numbers, null).
export function Select({ value, onChange, options, placeholder = null, disabled = false, class: cls = "", title }) {
  const opts = options.map((o) => (o !== null && typeof o === "object" ? o : { value: o, label: String(o) }));
  const idx = opts.findIndex((o) => o.value === value);
  const missing = idx < 0 && value != null && value !== "";
  return html`<div class=${cx("select", cls)} title=${title}>
    <select class="input" disabled=${disabled}
      onChange=${(e) => {
        const i = Number(e.currentTarget.value);
        if (i >= 0 && opts[i]) onChange(opts[i].value);
      }}>
      ${idx < 0 && html`<option value="-1" selected disabled>${missing ? `（${value}，找不到）` : placeholder ?? "（未選擇）"}</option>`}
      ${opts.map((o, i) => html`<option value=${i} selected=${i === idx} disabled=${o.disabled}>${o.label}</option>`)}
    </select>
    <${Icon} name="chevron-down" size=${14} class="select-caret" />
  </div>`;
}

export function Toggle({ checked, onChange, label = "", hint = "", disabled = false, class: cls = "" }) {
  return html`<label class=${cx("toggle", disabled && "is-disabled", cls)}>
    <input type="checkbox" checked=${!!checked} disabled=${disabled} onChange=${(e) => onChange(e.currentTarget.checked)} />
    <span class="toggle-track"><span class="toggle-thumb"></span></span>
    ${(label || hint) && html`<span class="toggle-text">
      ${label && html`<span class="toggle-label">${label}</span>`}
      ${hint && html`<span class="toggle-hint">${hint}</span>`}
    </span>`}
  </label>`;
}

export function Segmented({ value, onChange, options, size = "md", disabled = false, class: cls = "" }) {
  return html`<div class=${cx("seg", size === "sm" && "seg-sm", cls)} role="radiogroup">
    ${options.map((o) => html`<button type="button" role="radio" aria-checked=${o.value === value ? "true" : "false"}
        class=${cx("seg-btn", o.value === value && "active")} title=${o.title} disabled=${disabled || o.disabled}
        onClick=${() => o.value !== value && onChange(o.value)}>
      ${o.icon && html`<${Icon} name=${o.icon} size=${size === "sm" ? 13 : 15} />`}
      ${o.label && html`<span>${o.label}</span>`}
    </button>`)}
  </div>`;
}

export function Slider({ value, onChange, onCommit, min = 0, max = 1, step = 0.01, format = null, disabled = false, class: cls = "" }) {
  const [local, setLocal] = useState(value);
  const dragging = useRef(false);
  useEffect(() => {
    if (!dragging.current) setLocal(value);
  }, [value]);
  const pct = max > min ? ((clamp(local, min, max) - min) / (max - min)) * 100 : 0;
  return html`<div class=${cx("slider", cls)}>
    <input type="range" min=${min} max=${max} step=${step} value=${local} disabled=${disabled} style=${`--pct:${pct}%`}
      onInput=${(e) => {
        dragging.current = true;
        const v = Number(e.currentTarget.value);
        setLocal(v);
        if (onChange) onChange(v);
      }}
      onChange=${(e) => {
        dragging.current = false;
        if (onCommit) onCommit(Number(e.currentTarget.value));
      }} />
    ${format && html`<span class="slider-val">${format(local)}</span>`}
  </div>`;
}

export function Tabs({ value, onChange, tabs, class: cls = "" }) {
  return html`<div class=${cx("tabs", cls)} role="tablist">
    ${tabs.map((t) => html`<button type="button" role="tab" aria-selected=${t.id === value ? "true" : "false"} title=${t.title}
        class=${cx("tab", t.id === value && "active")} onClick=${() => onChange(t.id)}>
      ${t.icon && html`<${Icon} name=${t.icon} size=${15} />`}
      <span>${t.label}</span>
      ${t.badge != null && t.badge !== 0 && t.badge !== "" && html`<span class="tab-badge">${t.badge}</span>`}
    </button>`)}
  </div>`;
}

export function Badge({ tone = "neutral", children, title, class: cls = "" }) {
  return html`<span class=${cx("badge", `badge-${tone}`, cls)} title=${title}>${children}</span>`;
}

export function Dot({ tone = "neutral", pulse = false, title }) {
  return html`<span class=${cx("dot", `dot-${tone}`, pulse && "pulse")} title=${title}></span>`;
}

export function Progress({ value = null, tone = "accent", class: cls = "" }) {
  const width = value == null ? "" : `width:${round(clamp(value, 0, 1) * 100, 1)}%`;
  return html`<div class=${cx("progress", `progress-${tone}`, value == null && "indeterminate", cls)}><i style=${width}></i></div>`;
}

export function Empty({ icon = "inbox", title, children, actions = null, class: cls = "" }) {
  return html`<div class=${cx("empty", cls)}>
    <div class="empty-icon"><${Icon} name=${icon} size=${26} /></div>
    ${title && html`<div class="empty-title">${title}</div>`}
    ${children && html`<div class="empty-text">${children}</div>`}
    ${actions && html`<div class="empty-actions">${actions}</div>`}
  </div>`;
}

const KEY_LABEL = {
  mod: MOD_LABEL, shift: "Shift", alt: "Alt", escape: "Esc", space: "Space", enter: "Enter", delete: "Del",
  backspace: "Backspace", arrowleft: "←", arrowright: "→", arrowup: "↑", arrowdown: "↓", home: "Home", end: "End",
  tab: "Tab",
};

export const comboLabel = (combo) => comboParts(combo).map((k) => KEY_LABEL[k] || k.toUpperCase()).join("+");

export function Kbd({ children }) {
  return html`<kbd class="kbd">${children}</kbd>`;
}

export function Keys({ combo }) {
  return html`<span class="keys">${comboParts(combo).map((k) =>html`<${Kbd}>${KEY_LABEL[k] || k.toUpperCase()}</${Kbd}>`)}</span>`;
}

export function Chip({ active = false, onClick, icon = null, title, tone = null, disabled = false, children, class: cls = "" }) {
  return html`<button type="button" title=${title} disabled=${disabled} onClick=${onClick}
      class=${cx("chip", active && "active", tone && `chip-${tone}`, cls)}>
    ${icon && html`<${Icon} name=${icon} size=${13} />`}
    ${children != null && html`<span>${children}</span>`}
  </button>`;
}

function readOpen(id, fallback) {
  try {
    const v = localStorage.getItem(`vc.section.${id}`);
    return v == null ? fallback : v === "1";
  } catch {
    return fallback;
  }
}

export function Section({ title, icon = null, actions = null, collapsible = false, defaultOpen = true, id = null,
  children, class: cls = "" }) {
  const [open, setOpen] = useState(() => (collapsible && id ? readOpen(id, defaultOpen) : defaultOpen));
  const toggle = () => {
    setOpen(!open);
    try {
      if (id) localStorage.setItem(`vc.section.${id}`, open ? "0" : "1");
    } catch {
      // Storage can be blocked; the section still toggles for this session.
    }
  };
  const head = html`${icon && html`<${Icon} name=${icon} size=${15} />`}<span>${title}</span>`;
  return html`<section class=${cx("section", open ? "open" : "closed", cls)}>
    ${title && html`<div class="section-head">
      ${collapsible
        ? html`<button type="button" class="section-toggle" aria-expanded=${open ? "true" : "false"} onClick=${toggle}>
            <${Icon} name="chevron-right" size=${14} class="section-caret" />${head}
          </button>`
        : html`<div class="section-title">${head}</div>`}
      ${actions && html`<div class="section-actions">${actions}</div>`}
    </div>`}
    ${open && html`<div class="section-body">${children}</div>`}
  </section>`;
}
