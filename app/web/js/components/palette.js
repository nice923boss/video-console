// Command palette (Ctrl+Shift+P by default): ranked search over actions, views, shots and projects.
import { html, useState, useMemo, useRef, useEffect, useLayoutEffect } from "../lib/h.js";
import { keysOf, useDialogKeys } from "../lib/keymap.js";
import { Icon } from "./icons.js";
import { Keys } from "./ui.js";
import { openModal } from "./overlay.js";
import { toastError } from "./toast.js";

const RECENT_KEY = "vc.palette.recent";
const RECENT_MAX = 5;
const GROUP_CAP = 12; // rows per group while the query is empty, until the group is opened in full
const RESULT_CAP = 80;
const BOUNDARY = /[\s\-_/.:：，,（(「]/;

function loadRecent() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function saveRecent(id) {
  try {
    const list = [id, ...loadRecent().filter((x) => x !== id)].slice(0, RECENT_MAX);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch {
    // Storage can be unavailable; recents are a convenience only.
  }
}

function isSubsequence(text, term) {
  let i = 0;
  for (const ch of text) {
    if (ch === term[i]) i += 1;
    if (i === term.length) return true;
  }
  return false;
}

// Higher is better; -1 means some term matched nothing.
function scoreOf(cmd, terms) {
  const label = cmd.label.toLowerCase();
  const extra = `${cmd.keywords || ""} ${cmd.group || ""} ${cmd.detail || ""}`.toLowerCase();
  let total = 0;
  for (const t of terms) {
    const at = label.indexOf(t);
    if (at === 0) total += 100;
    else if (at > 0) total += BOUNDARY.test(label[at - 1]) ? 70 : 50;
    else if (extra.includes(t)) total += 30;
    else if (t.length > 1 && isSubsequence(label, t)) total += 10;
    else return -1;
  }
  return total - label.length / 100; // shorter labels win ties
}

function buildRows(commands, query, recent, expanded) {
  const usable = commands.filter((c) => c && c.label);
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length) {
    return usable
      .map((c) => ({ c, s: scoreOf(c, terms) }))
      .filter((x) => x.s >= 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, RESULT_CAP)
      .map((x) => ({ cmd: x.c, tag: x.c.group }));
  }
  const rows = [];
  const byId = new Map(usable.map((c) => [c.id, c]));
  const recents = recent.map((id) => byId.get(id)).filter((c) => c && !c.disabled);
  if (recents.length) {
    rows.push({ header: "最近使用" });
    for (const c of recents) rows.push({ cmd: c, tag: c.group, key: `r:${c.id}` });
  }
  const groups = new Map();
  for (const c of usable) {
    const g = c.group || "其他";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  }
  for (const [group, list] of groups) {
    rows.push({ header: group });
    const cap = expanded.has(group) ? list.length : GROUP_CAP;
    for (const c of list.slice(0, cap)) rows.push({ cmd: c });
    if (list.length > cap) rows.push({ more: list.length - cap, group });
  }
  return rows;
}

function Palette({ commands, close, placeholder }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0); // index into picks
  const [expanded, setExpanded] = useState(() => new Set());
  const recent = useMemo(loadRecent, []);
  const rows = useMemo(() => buildRows(commands, query.trim(), recent, expanded), [commands, query, recent, expanded]);
  const picks = useMemo(() => rows.map((r, i) => ((r.cmd && !r.cmd.disabled) || r.more ? i : -1)).filter((i) => i >= 0), [rows]);
  const listRef = useRef(null);
  const inputRef = useRef(null);
  const downOnBack = useRef(false);
  const current = picks.length ? picks[Math.min(active, picks.length - 1)] : null;

  useEffect(() => {
    const prev = document.activeElement;
    if (inputRef.current) inputRef.current.focus();
    return () => {
      if (prev && typeof prev.focus === "function" && document.contains(prev)) prev.focus();
    };
  }, []);

  useLayoutEffect(() => {
    const el = listRef.current && listRef.current.querySelector(".active");
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [current, rows]);

  const run = (i) => {
    const row = rows[i];
    if (row && row.more) {
      setExpanded((s) => new Set([...s, row.group]));
      return;
    }
    if (!row || !row.cmd || row.cmd.disabled) return;
    saveRecent(row.cmd.id);
    close(row.cmd);
  };
  const move = (d) => {
    if (picks.length) setActive((a) => (Math.min(a, picks.length - 1) + d + picks.length) % picks.length);
  };
  const closeKeys = Object.fromEntries(keysOf("app.palette").map((k) => [k, { fn: () => close(null), allowInInput: true }]));
  useDialogKeys({
    ...closeKeys,
    "dialog.close": { fn: () => close(null), allowInInput: true },
    arrowdown: { fn: () => move(1), allowInInput: true },
    arrowup: { fn: () => move(-1), allowInInput: true },
    tab: { fn: () => move(1), allowInInput: true },
    "shift+tab": { fn: () => move(-1), allowInInput: true },
    enter: { fn: () => (current != null ? run(current) : false), allowInInput: true },
  }, { modal: true });

  const onInput = (e) => {
    setQuery(e.currentTarget.value);
    setActive(0);
  };
  const hover = (i) => {
    const k = picks.indexOf(i);
    if (k >= 0 && picks[active] !== i) setActive(k);
  };
  const q = query.trim();

  return html`<div class="modal-back palette-back"
      onMouseDown=${(e) => { downOnBack.current = e.target === e.currentTarget; }}
      onMouseUp=${(e) => {
        if (downOnBack.current && e.target === e.currentTarget) close(null);
        downOnBack.current = false;
      }}>
    <div class="palette" role="dialog" aria-modal="true" aria-label="指令面板">
      <div class="pal-search">
        <${Icon} name="search" size=${18} />
        <input ref=${inputRef} class="pal-input" type="text" value=${query} placeholder=${placeholder}
          spellcheck="false" autocomplete="off" role="combobox" aria-expanded="true" aria-controls="pal-list"
          aria-activedescendant=${current != null ? `pal-${current}` : undefined} onInput=${onInput} />
        <${Keys} combo="escape" />
      </div>
      <div class="pal-list" id="pal-list" role="listbox" ref=${listRef}>
        ${rows.map((r, i) => {
          if (r.header) return html`<div class="pal-head" key=${`h:${r.header}`}>${r.header}</div>`;
          if (r.more) return html`<button type="button" tabindex="-1" id=${`pal-${i}`} key=${`m:${r.group}`} role="option"
              aria-selected=${i === current} class=${`pal-more ${i === current ? "active" : ""}`}
              onMouseMove=${() => hover(i)} onClick=${() => run(i)}>顯示其餘 ${r.more} 項（或輸入關鍵字縮小範圍）</button>`;
          const c = r.cmd;
          const on = i === current;
          return html`<button type="button" tabindex="-1" id=${`pal-${i}`} key=${r.key || c.id} role="option"
              aria-selected=${on} disabled=${c.disabled}
              class=${`pal-item ${on ? "active" : ""} ${c.danger ? "danger" : ""}`}
              onMouseMove=${() => hover(i)} onClick=${() => run(i)}>
            <span class="pal-icon">
              ${c.thumb ? html`<img src=${c.thumb} alt="" loading="lazy" />` : html`<${Icon} name=${c.icon || "command"} size=${16} />`}
            </span>
            <span class="pal-label">
              <span>${c.label}</span>
              ${c.detail && html`<small>${c.detail}</small>`}
            </span>
            ${r.tag && html`<span class="pal-tag">${r.tag}</span>`}
            ${c.hint && html`<${Keys} combo=${c.hint} />`}
          </button>`;
        })}
        ${!picks.length && html`<div class="pal-empty">
          <${Icon} name="search" size=${22} />
          <span>${q ? `找不到符合「${q}」的項目` : "目前沒有可用的指令"}</span>
        </div>`}
      </div>
      <footer class="pal-foot">
        <span><${Keys} combo="arrowup" /><${Keys} combo="arrowdown" />選擇</span>
        <span><${Keys} combo="enter" />執行</span>
        <span><${Keys} combo="escape" />關閉</span>
      </footer>
    </div>
  </div>`;
}

// commands: [{id, label, group, icon, hint, keywords, detail, thumb, disabled, danger, run}]
// The command runs after the palette has closed, so it may open its own dialog.
export function openPalette(commands, { placeholder = "搜尋指令、鏡頭或專案" } = {}) {
  return openModal((close) => html`<${Palette} commands=${commands} close=${close} placeholder=${placeholder} />`)
    .then((cmd) => (cmd ? cmd.run() : null))
    .catch((err) => {
      toastError(err);
    });
}
