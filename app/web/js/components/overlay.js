// Modal dialogs and context menus. One host at the app root renders whatever is open; callers get
// a promise (dialogs) or fire-and-forget (menus).
import { html, useState, useEffect, useRef, useLayoutEffect } from "../lib/h.js";
import { useHotkeys } from "../lib/keys.js";
import { Icon } from "./icons.js";

let modals = []; // [{id, render, close}]
let menu = null; // {id, x, y, flipY, minWidth, items}
let seq = 0;
const subs = new Set();

function emit() {
  for (const fn of [...subs]) fn();
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// render(close) returns the dialog; close(value) resolves the promise with value.
export function openModal(render) {
  return new Promise((resolve) => {
    const id = (seq += 1);
    const close = (value) => {
      if (!modals.some((m) => m.id === id)) return;
      modals = modals.filter((m) => m.id !== id);
      emit();
      resolve(value);
    };
    modals = [...modals, { id, render, close }];
    menu = null;
    emit();
  });
}

// backdropClose=false keeps a dialog the user types or edits in open when a stray click lands beside it;
// it then closes only from its own buttons, the X or Esc.
export function Modal({ title, onClose, width = null, footer = null, children, class: cls = "", wide = false, backdropClose = true }) {
  const ref = useRef(null);
  const downOnBack = useRef(false);
  useHotkeys({ escape: { fn: () => onClose(), allowInInput: true } }, { modal: true });

  useEffect(() => {
    const prev = document.activeElement;
    const el = ref.current;
    const target = el && (el.querySelector("[data-autofocus]")
      || el.querySelector(".modal-body input:not([type=hidden]):not([disabled]), .modal-body textarea:not([disabled])")
      || el.querySelector(".modal-foot .btn-primary, .modal-foot .btn-danger")
      || el.querySelector(FOCUSABLE));
    if (target) target.focus();
    return () => {
      if (prev && typeof prev.focus === "function" && document.contains(prev)) prev.focus();
    };
  }, []);

  // Keep Tab inside the dialog.
  const onKeyDown = (e) => {
    if (e.key !== "Tab" || !ref.current) return;
    const nodes = [...ref.current.querySelectorAll(FOCUSABLE)].filter((n) => n.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const size = width ?? (wide ? 820 : 480);
  const w = typeof size === "number" ? `${size}px` : size;
  return html`<div class="modal-back"
      onMouseDown=${(e) => { downOnBack.current = e.target === e.currentTarget; }}
      onMouseUp=${(e) => {
        if (backdropClose && downOnBack.current && e.target === e.currentTarget) onClose();
        downOnBack.current = false;
      }}>
    <div class=${`modal ${wide ? "modal-wide" : ""} ${cls}`} role="dialog" aria-modal="true" aria-label=${title || undefined}
      ref=${ref} style=${`width:min(${w}, calc(100vw - 32px))`} onKeyDown=${onKeyDown}>
      ${title != null && html`<header class="modal-head">
        <h2>${title}</h2>
        <button type="button" class="icon-btn" title="關閉（Esc）" aria-label="關閉" onClick=${() => onClose()}>
          <${Icon} name="x" />
        </button>
      </header>`}
      <div class="modal-body">${children}</div>
      ${footer && html`<footer class="modal-foot">${footer}</footer>`}
    </div>
  </div>`;
}

export function confirmDialog({ title, message = "", confirmLabel = "確定", cancelLabel = "取消", danger = false }) {
  return openModal((close) => html`<${Modal} title=${title} width=${440} onClose=${() => close(false)}
      footer=${html`
        <button type="button" class="btn" onClick=${() => close(false)}>${cancelLabel}</button>
        <button type="button" class=${`btn ${danger ? "btn-danger" : "btn-primary"}`} onClick=${() => close(true)}>
          ${confirmLabel}
        </button>`}>
      ${message && html`<p class="modal-text">${message}</p>`}
    </${Modal}>`).then((v) => v === true);
}

function PromptBody({ opts, close }) {
  const { title, label = "", value = "", placeholder = "", confirmLabel = "確定", multiline = false, validate = null, hint = "" } = opts;
  const [text, setText] = useState(value || "");
  const [error, setError] = useState("");
  const submit = () => {
    const v = text.trim();
    const err = validate ? validate(v) : v ? "" : "請輸入內容";
    if (err) {
      setError(err);
      return;
    }
    close(v);
  };
  const onKeyDown = (e) => {
    if (e.isComposing || e.keyCode === 229 || e.key !== "Enter") return;
    if (multiline && !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    submit();
  };
  const onInput = (e) => {
    setText(e.currentTarget.value);
    if (error) setError("");
  };
  const props = { value: text, placeholder, onInput, onKeyDown, "data-autofocus": true, class: "input" };
  return html`<${Modal} title=${title} width=${multiline ? 560 : 440} onClose=${() => close(null)} backdropClose=${false}
      footer=${html`
        <button type="button" class="btn" onClick=${() => close(null)}>取消</button>
        <button type="button" class="btn btn-primary" onClick=${submit}>${confirmLabel}</button>`}>
      <label class="field">
        ${label && html`<span class="field-label">${label}</span>`}
        ${multiline
          ? html`<textarea ...${props} rows="6"></textarea>`
          : html`<input ...${props} type="text" onFocus=${(e) => e.currentTarget.select()} />`}
        ${error ? html`<span class="field-error">${error}</span>` : hint && html`<span class="field-hint">${hint}</span>`}
      </label>
    </${Modal}>`;
}

// Resolves with the trimmed text, or null when canceled.
export function promptDialog(opts) {
  return openModal((close) => html`<${PromptBody} opts=${opts} close=${close} />`);
}

// ---------------------------------------------------------------- menus
// target: a contextmenu event (opens at the pointer), a click event or element (opens below it),
// or {x, y}. items: [{label, icon, onClick, danger, disabled, hint, checked, divider, header}]
export function openMenu(target, items) {
  let x = 8;
  let y = 8;
  let flipY = null;
  let minWidth = 0;
  if (target && typeof target.preventDefault === "function") {
    if (target.type === "contextmenu") {
      target.preventDefault();
      x = target.clientX;
      y = target.clientY;
    } else {
      target.stopPropagation();
      target = target.currentTarget;
    }
  }
  if (target && typeof target.getBoundingClientRect === "function") {
    const r = target.getBoundingClientRect();
    x = r.left;
    y = r.bottom + 4;
    flipY = r.top - 4;
    minWidth = Math.min(r.width, 320);
  } else if (target && typeof target.x === "number" && typeof target.y === "number") {
    x = target.x;
    y = target.y;
  }
  menu = { id: (seq += 1), x, y, flipY, minWidth, items: items.filter(Boolean), prev: document.activeElement };
  emit();
}

export function closeMenu() {
  if (!menu) return;
  const prev = menu.prev;
  menu = null;
  emit();
  if (prev && typeof prev.focus === "function" && document.contains(prev)) prev.focus();
}

const usable = (it) => it && !it.divider && !it.header && !it.disabled;

function MenuView({ m }) {
  const ref = useRef(null);
  const [pos, setPos] = useState(null);
  const [active, setActive] = useState(-1);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    let left = Math.min(m.x, window.innerWidth - w - 8);
    let top = m.y;
    if (top + h > window.innerHeight - 8) {
      top = m.flipY != null && m.flipY - h >= 8 ? m.flipY - h : Math.max(8, window.innerHeight - h - 8);
    }
    left = Math.max(8, left);
    setPos({ left, top });
    el.focus();
  }, [m.id]);

  useEffect(() => {
    const outside = (e) => {
      if (ref.current && !ref.current.contains(e.target)) closeMenu();
    };
    const onScroll = (e) => {
      if (ref.current && !ref.current.contains(e.target)) closeMenu();
    };
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", closeMenu);
    window.addEventListener("blur", closeMenu);
    return () => {
      document.removeEventListener("pointerdown", outside, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", closeMenu);
      window.removeEventListener("blur", closeMenu);
    };
  }, [m.id]);

  const run = (it) => {
    if (!usable(it)) return;
    closeMenu();
    if (it.onClick) it.onClick();
  };
  const moveBy = (dir) => {
    const n = m.items.length;
    let i = active;
    for (let k = 0; k < n; k += 1) {
      i = (i + dir + n) % n;
      if (usable(m.items[i])) break;
    }
    setActive(i);
  };
  useHotkeys({
    escape: { fn: () => closeMenu(), allowInInput: true },
    arrowdown: { fn: () => moveBy(1), allowInInput: true },
    arrowup: { fn: () => moveBy(-1), allowInInput: true },
    enter: { fn: () => (active >= 0 ? run(m.items[active]) : false), allowInInput: true },
    tab: { fn: () => closeMenu(), allowInInput: true },
  }, { modal: true });

  const style = pos ? `left:${pos.left}px;top:${pos.top}px;min-width:${Math.max(m.minWidth, 180)}px`
    : `left:-9999px;top:0;visibility:hidden`;
  return html`<div class="menu" role="menu" tabindex="-1" ref=${ref} style=${style}
      onContextMenu=${(e) => e.preventDefault()}>
    ${m.items.map((it, i) => {
      if (it.divider) return html`<div class="menu-sep" role="separator" key=${`d${i}`}></div>`;
      if (it.header) return html`<div class="menu-head" key=${`h${i}`}>${it.header}</div>`;
      const cls = `menu-item ${it.danger ? "danger" : ""} ${i === active ? "active" : ""}`;
      return html`<button type="button" key=${i} class=${cls} role="menuitem" disabled=${it.disabled}
          title=${it.title || undefined} onMouseEnter=${() => setActive(i)} onClick=${() => run(it)}>
        <span class="menu-icon">
          ${it.checked != null ? it.checked && html`<${Icon} name="check" size=${15} />`
            : it.icon && html`<${Icon} name=${it.icon} size=${15} />`}
        </span>
        <span class="menu-label">${it.label}</span>
        ${it.hint && html`<span class="menu-hint">${it.hint}</span>`}
      </button>`;
    })}
  </div>`;
}

export function OverlayHost() {
  const [, force] = useState(0);
  useEffect(() => {
    const fn = () => force((n) => n + 1);
    subs.add(fn);
    return () => subs.delete(fn);
  }, []);
  return html`
    ${modals.map((m) => html`<div class="modal-layer" key=${m.id}>${m.render(m.close)}</div>`)}
    ${menu && html`<${MenuView} key=${menu.id} m=${menu} />`}
  `;
}
