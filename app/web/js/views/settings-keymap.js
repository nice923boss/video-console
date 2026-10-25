// Settings → 快捷鍵: every rebindable command with its keys. Click a key to record a new one, add more
// keys per command, see which other command already uses a key, and restore one command or all of
// them. Changes save to settings.ui.keymap at once (see lib/keymap.js).
import { html, useState, useEffect, useMemo } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { saveSettings } from "../lib/actions.js";
import { comboOf } from "../lib/keys.js";
import { GROUPS, PRESETS, RESERVED, allCommands, commandOf, conflictsOf, keysOf, presetKeys, readKeymap,
  selectKeymap, winnerOf } from "../lib/keymap.js";
import { Button, IconButton, Keys, Segmented, comboLabel } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { confirmDialog } from "../components/overlay.js";

// Pressed alone these only start a combo, so recording waits for the next key.
const MODIFIERS = new Set(["control", "shift", "alt", "meta", "os", "altgraph", "capslock", "fn"]);

function save(km) {
  saveSettings({ ui: { keymap: { preset: km.preset, overrides: [...km.overrides] } } });
}

// Keys equal to the preset drop the override, so the command keeps following preset switches.
function withKeys(km, id, keys) {
  const overrides = new Map(km.overrides);
  const base = presetKeys(commandOf(id), km.preset);
  if (keys.length === base.length && keys.every((k, i) => k === base[i])) overrides.delete(id);
  else overrides.set(id, keys);
  return { ...km, overrides };
}

function withoutOverride(km, id) {
  const overrides = new Map(km.overrides);
  overrides.delete(id);
  return { ...km, overrides };
}

function matches(c, keys, q) {
  if (!q) return true;
  if (c.label.toLowerCase().includes(q)) return true;
  return keys.some((k) => k.includes(q) || comboLabel(k).toLowerCase().includes(q));
}

// rec: {id, index, note} while recording; index -1 adds a key instead of replacing one.
function useRecorder(km) {
  const [rec, setRec] = useState(null);
  useEffect(() => {
    if (!rec) return undefined;
    const onKey = (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      e.stopPropagation();
      if (MODIFIERS.has((e.key || "").toLowerCase())) return;
      const combo = comboOf(e);
      if (combo === "escape") {
        setRec(null);
        return;
      }
      if (RESERVED.has(combo)) {
        setRec({ ...rec, note: `${comboLabel(combo)} 由瀏覽器保留，網頁攔不到，請換一組` });
        return;
      }
      const keys = keysOf(rec.id, km);
      const next = rec.index < 0 ? [...keys, combo] : keys.map((k, i) => (i === rec.index ? combo : k));
      save(withKeys(km, rec.id, [...new Set(next)]));
      setRec(null);
    };
    // Capture on window runs before the app's own shortcut listener on document.
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [rec, km]);
  return [rec, setRec];
}

function Recording({ rec, onCancel }) {
  return html`<button type="button" class="km-rec" ref=${(el) => el && el.focus()} onBlur=${onCancel}
      title="按 Esc 取消">${rec.note || "按下新的按鍵…"}</button>`;
}

// Which command takes a shared key, page by page when the clash spans an app-wide command.
function clashWinner(c, others) {
  const all = [c, ...others];
  const name = (list) => `「${commandOf(winnerOf(list.map((o) => o.id))).label}」`;
  const pages = [...new Set(all.filter((o) => o.scope !== "global").map((o) => o.scope))];
  const globals = all.filter((o) => o.scope === "global");
  if (!pages.length || (pages.length === 1 && !globals.length)) return `按下時由${name(all)}作用`;
  const parts = pages.map((s) => `在${all.find((o) => o.scope === s).group}頁由${
    name(all.filter((o) => o.scope === s || o.scope === "global"))}作用`);
  if (globals.length > 1) parts.push(`其他頁面由${name(globals)}作用`);
  return parts.join("，");
}

function CommandRow({ c, km, rec, setRec }) {
  const keys = keysOf(c.id, km);
  const custom = km.overrides.has(c.id);
  const recording = (index) => rec && rec.id === c.id && rec.index === index;
  const start = (index) => setRec({ id: c.id, index, note: "" });
  const cancel = () => setRec(null);
  const clashes = keys.map((k) => [k, conflictsOf(c.id, k, km)]).filter(([, others]) => others.length);
  const clashing = new Set(clashes.map(([k]) => k));

  return html`<div class="km-row">
    <div class="km-label">
      <span>${c.label}</span>
      ${custom && html`<span class="km-custom">已自訂</span>`}
    </div>
    <div class="km-keys">
      ${keys.map((k, i) => (recording(i)
        ? html`<${Recording} key=${k} rec=${rec} onCancel=${cancel} />`
        : html`<span key=${k} class=${`km-key ${clashing.has(k) ? "warn" : ""}`}>
            <button type="button" title="按一下重新錄製" onClick=${() => start(i)}><${Keys} combo=${k} /></button>
            <button type="button" class="km-key-x" title="移除這組按鍵" aria-label="移除這組按鍵"
              onClick=${() => save(withKeys(km, c.id, keys.filter((x) => x !== k)))}><${Icon} name="x" size=${12} /></button>
          </span>`))}
      ${recording(-1)
        ? html`<${Recording} rec=${rec} onCancel=${cancel} />`
        : html`<button type="button" class="km-add" title="替這個指令新增一組按鍵" onClick=${() => start(-1)}>
            <${Icon} name="plus" size=${12} />新增</button>`}
    </div>
    <div class="km-actions">
      ${custom && html`<${IconButton} icon="undo" size=${15} title="還原成預設組的按鍵"
        onClick=${() => save(withoutOverride(km, c.id))} />`}
    </div>
    ${clashes.map(([k, others]) => html`<div class="km-warn" key=${k}>
      <${Icon} name="alert" size=${13} />
      <span>${comboLabel(k)} 同時用在${others.map((o) => `「${o.label}」`).join("、")}，${clashWinner(c, others)}</span>
    </div>`)}
  </div>`;
}

export function KeymapEditor() {
  const raw = useStore(selectKeymap);
  const km = useMemo(() => readKeymap(raw), [raw]);
  const [query, setQuery] = useState("");
  const [rec, setRec] = useRecorder(km);
  const q = query.trim().toLowerCase();
  const commands = allCommands();
  const found = commands.some((c) => matches(c, keysOf(c.id, km), q));

  const resetAll = async () => {
    const preset = PRESETS.find((p) => p.value === km.preset).label;
    const ok = await confirmDialog({ title: "全部還原？", message: `所有自訂的按鍵都會回到「${preset}」預設組。`,
      confirmLabel: "全部還原", danger: true });
    if (ok) save({ preset: km.preset, overrides: new Map() });
  };

  return html`<div class="km">
    <div class="km-toolbar">
      <${Segmented} size="sm" value=${km.preset} options=${PRESETS} onChange=${(preset) => save({ ...km, preset })} />
      <label class="search-box">
        <${Icon} name="search" size=${15} />
        <input type="search" placeholder="搜尋指令或按鍵" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
      </label>
      <span class="grow"></span>
      <${Button} size="sm" kind="ghost" icon="undo" disabled=${!km.overrides.size} onClick=${resetAll}>全部還原<//>
    </div>
    <p class="km-note">按一下按鍵可以重新錄製，按 Esc 取消。Ctrl+T、Ctrl+W、Ctrl+N 等由瀏覽器保留，網頁攔不到。
      自訂過的指令不受預設組切換影響。</p>
    ${GROUPS.map((g) => {
      const rows = commands.filter((c) => c.group === g && matches(c, keysOf(c.id, km), q));
      return rows.length > 0 && html`<section class="km-group" key=${g}>
        <h4>${g}</h4>
        ${rows.map((c) => html`<${CommandRow} key=${c.id} c=${c} km=${km} rec=${rec} setRec=${setRec} />`)}
      </section>`;
    })}
    ${!found && html`<p class="km-note">找不到符合「${query.trim()}」的指令</p>`}
  </div>`;
}
