// Caption styles: the project's named styles (each caption picks one, the project has a default) and the
// app-wide library styles are saved to and added from. Project changes are ops, so they are undoable and travel
// with the project package; the library lives in the app settings. The sample is drawn with the same CSS as
// the player preview (lib/caption-style.js), which mirrors the burnt-in export.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { ops, saveSettings } from "../lib/actions.js";
import { api } from "../lib/api.js";
import { newId, round } from "../lib/format.js";
import {
  DEFAULT_STYLE, MAX_STYLES, legacySize, styleById, defaultStyleId, resolveStyle, textCss, blockCss, zonePad,
  anchorOf, editStyleOps, addStyleOps, setDefaultOps, useForAllOps,
} from "../lib/caption-style.js";
import { PRESET_STYLES } from "../lib/caption-presets.js";
import { openModal, Modal, confirmDialog } from "../components/overlay.js";
import { Button, IconButton, Field, DraftText, NumberInput, Select, Toggle, Segmented, Badge } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast } from "../components/toast.js";

const NONE = [];
const SAMPLE_TEXT = "字幕樣式預覽 Caption 123";
const PRESETS = PRESET_STYLES.map((p) => ({ ...DEFAULT_STYLE, ...p }));
const ALIGNS = [{ value: "left", label: "靠左" }, { value: "center", label: "置中" }, { value: "right", label: "靠右" }];
const cx = (...parts) => parts.filter(Boolean).join(" ");

// ---------------------------------------------------------------- installed fonts (read once per session)
let fontsPromise = null;
function loadFonts() {
  if (!fontsPromise) {
    fontsPromise = api.get("/api/system/fonts").then((r) => r.fonts || []).catch((err) => {
      fontsPromise = null;
      throw err;
    });
  }
  return fontsPromise;
}

function useFonts() {
  const [fonts, setFonts] = useState({ list: null, error: "" });
  useEffect(() => {
    let live = true;
    loadFonts().then(
      (list) => live && setFonts({ list, error: "" }),
      (err) => live && setFonts({ list: [], error: (err && err.message) || String(err) }),
    );
    return () => { live = false; };
  }, []);
  return fonts;
}

// A colour swatch: the picker previews locally and saves after a pause (and when the dialog closes).
function ColorInput({ value, onCommit, title, disabled }) {
  const [local, setLocal] = useState(value);
  const pending = useRef(null);
  const timer = useRef(0);
  const commit = useRef(onCommit);
  commit.current = onCommit;
  useEffect(() => {
    if (pending.current == null) setLocal(value);
  }, [value]);
  const flush = () => {
    clearTimeout(timer.current);
    const v = pending.current;
    pending.current = null;
    if (v != null) commit.current(v);
  };
  useEffect(() => flush, []);
  const change = (v) => {
    setLocal(v);
    pending.current = v;
    clearTimeout(timer.current);
    timer.current = setTimeout(flush, 250);
  };
  return html`<label class=${cx("route-color cs-color", disabled && "is-disabled")} style=${`--route:${local}`} title=${title}>
    <input type="color" value=${local} disabled=${disabled} onInput=${(e) => change(e.currentTarget.value)} />
  </label>`;
}

const Percent = ({ value, onCommit, disabled, title }) => html`<${NumberInput} value=${round(value * 100, 0)} min=${0} max=${100}
  step=${5} suffix="%" disabled=${disabled} title=${title} onCommit=${(v) => onCommit(round(v / 100, 3))} />`;

// The little "字" in the style list: colour, outline and box at a glance.
function swatchCss(s) {
  const font = String(s.font || DEFAULT_STYLE.font).replace(/["\\;{}<>]/g, "");
  return [
    `font-family:"${font}","${DEFAULT_STYLE.font}",sans-serif`,
    `font-weight:${s.bold ? 700 : 400}`,
    `font-style:${s.italic ? "italic" : "normal"}`,
    `color:${s.color}`,
    s.box ? `background:${s.boxColor}` : "",
    s.outline && s.outlineWidth > 0 ? `-webkit-text-stroke:2px ${s.outlineColor};paint-order:stroke fill` : "",
  ].filter(Boolean).join(";");
}

function StyleItem({ s, active, isDefault, count, onSelect }) {
  return html`<li><button type="button" class=${cx("cs-item", active && "active")} onClick=${onSelect}>
    <span class="cs-swatch" style=${swatchCss(s)}>字</span>
    <span class="cs-item-name">${s.name}</span>
    ${isDefault && html`<${Badge} tone="accent" title="沒有指定樣式的字幕都用這個">預設<//>`}
    ${count > 0 && html`<span class="cs-item-count dim">${count} 句</span>`}
  </button></li>`;
}

function Sample({ s, text, row, ratio }) {
  const point = anchorOf(s, null);
  const cap = html`<div class="pl-cap" style=${blockCss(s, point)}>
    <span class="pl-cap-text" style=${textCss(s)}>${text}</span></div>`;
  // Fixed height, so the sample stays in view (sticky) while the sections below it scroll.
  return html`<div class="cs-stage"><div class="cs-sample" style=${`aspect-ratio:${ratio}`} title="預覽；實際效果以匯出畫面為準">
    ${point ? cap : html`<div class=${`pl-cap-zone pos-${row}`} style=${zonePad(row, [s])}>${cap}</div>`}
  </div></div>`;
}

function Section({ title, toggle, children }) {
  return html`<section class="cs-section">
    <header class="cs-section-head">${toggle || html`<span class="cs-section-title">${title}</span>`}</header>
    <div class="cs-fields">${children}</div>
  </section>`;
}

function Editor({ s, off, edit, fonts, text, row, ratio }) {
  const fontOptions = (() => {
    const list = (fonts.list || NONE).map((f) => ({ value: f.name, label: f.label === f.name ? f.name : `${f.label}（${f.name}）` }));
    return list.some((o) => o.value === s.font) ? list : [{ value: s.font, label: s.font }, ...list];
  })();
  const num = (key, label, opts) => html`<${Field} label=${label} hint=${opts.hint}>
    <${NumberInput} value=${s[key]} min=${opts.min} max=${opts.max} step=${opts.step || 1} suffix=${opts.suffix ?? "px"}
      digits=${opts.digits ?? 1} disabled=${off || opts.disabled} onCommit=${(v) => edit({ [key]: v }, opts.label, key)} /><//>`;
  const color = (key, label) => html`<${Field} label=${label}>
    <${ColorInput} value=${s[key]} disabled=${off} title=${label} onCommit=${(v) => edit({ [key]: v }, "字幕樣式顏色", key)} /><//>`;
  const pct = (key, label) => html`<${Field} label=${label}>
    <${Percent} value=${s[key]} disabled=${off} onCommit=${(v) => edit({ [key]: v }, "字幕樣式不透明度", key)} /><//>`;
  const point = anchorOf(s, null);

  return html`<div class="cs-editor">
    <${Sample} s=${s} text=${text} row=${row} ratio=${ratio} />
    <${Section} title="文字">
      <${Field} label="名稱" class="cs-span2">
        <${DraftText} value=${s.name} maxLength=${40} disabled=${off}
          onCommit=${(v) => edit({ name: v.trim() || s.name }, "重新命名字幕樣式", "name")} /><//>
      <${Field} label="字型" class="cs-span2" error=${fonts.error && `無法讀取已安裝字型：${fonts.error}`}>
        <${Select} value=${s.font} options=${fontOptions} disabled=${off} title="電腦上已安裝的字型；匯出時用同一個字型燒進畫面"
          onChange=${(font) => edit({ font }, "字幕字型")} /><//>
      ${num("size", "大小", { min: 16, max: 200, step: 2, digits: 0, label: "字幕大小" })}
      ${num("spacing", "字距", { min: -10, max: 40, label: "字幕字距" })}
      ${color("color", "文字顏色")}
      ${pct("opacity", "文字不透明度")}
      <div class="cs-toggles cs-span4">
        <${Toggle} checked=${s.bold} disabled=${off} label="粗體" onChange=${(bold) => edit({ bold }, "字幕粗體")} />
        <${Toggle} checked=${s.italic} disabled=${off} label="斜體" onChange=${(italic) => edit({ italic }, "字幕斜體")} />
      </div>
    <//>
    <${Section} toggle=${html`<${Toggle} checked=${s.outline} disabled=${off} label="外框"
      onChange=${(outline) => edit({ outline }, "字幕外框")} />`}>
      ${color("outlineColor", "外框顏色")}
      ${num("outlineWidth", "外框粗細", { min: 0, max: 20, step: 0.5, label: "字幕外框粗細", disabled: !s.outline })}
    <//>
    <${Section} toggle=${html`<${Toggle} checked=${s.shadow} disabled=${off} label="陰影"
      onChange=${(shadow) => edit({ shadow }, "字幕陰影")} />`}>
      ${color("shadowColor", "陰影顏色")}
      ${pct("shadowOpacity", "陰影不透明度")}
      ${num("shadowDistance", "距離", { min: 0, max: 30, step: 0.5, label: "字幕陰影距離", hint: "0 為四周光暈", disabled: !s.shadow })}
      ${num("shadowBlur", "模糊", { min: 0, max: 20, step: 0.5, label: "字幕陰影模糊", disabled: !s.shadow })}
    <//>
    <${Section} toggle=${html`<${Toggle} checked=${s.box} disabled=${off} label="半透明底框"
      onChange=${(box) => edit({ box }, "字幕底框")} />`}>
      ${color("boxColor", "底框顏色")}
      ${pct("boxOpacity", "底框不透明度")}
      ${num("boxPadding", "內距", { min: 0, max: 60, label: "字幕底框內距", disabled: !s.box })}
    <//>
    <${Section} title="位置">
      <${Field} label="對齊" class="cs-span2">
        <${Segmented} value=${s.align} options=${ALIGNS} size="sm" disabled=${off} onChange=${(align) => edit({ align }, "字幕對齊")} /><//>
      ${num("marginV", "上下邊距", { min: 0, max: 500, step: 5, digits: 0, label: "字幕上下邊距", disabled: !!point })}
      ${num("marginH", "左右邊距", { min: 0, max: 600, step: 5, digits: 0, label: "字幕左右邊距" })}
      ${point && html`<div class="cs-moved cs-span4">
        <${Icon} name="move" size=${14} />
        <span>這個樣式已拖曳到自訂位置（${round(point.x * 100, 1)}%, ${round(point.y * 100, 1)}%），上下邊距暫不作用</span>
        <${Button} size="sm" kind="ghost" icon="refresh" disabled=${off}
          onClick=${() => edit({ x: null, y: null }, "重設字幕樣式位置")}>回到上中下位置<//>
      </div>`}
    <//>
  </div>`;
}

function StyleBody({ close, styleId, text, row }) {
  const { project, settings } = useStore((st) => ({ project: st.project, settings: st.settings }));
  const fonts = useFonts();
  const legacy = legacySize(settings);
  const defaultId = project ? defaultStyleId(project) : null;
  const [sel, setSel] = useState(() => (project && styleById(project, styleId) ? styleId : defaultId));
  const created = useRef(new Set()); // ids just added, shown as "being created" until the project reloads

  const style = project ? styleById(project, sel) : null;
  const pending = !!sel && !style && created.current.has(sel);
  const valid = style || pending || (!sel && !defaultId);
  useEffect(() => {
    if (!project) close(null);
    else if (style) created.current.delete(sel);
    else if (!valid) setSel(defaultId || ((project.captionStyles || NONE)[0] || {}).id || null);
  });
  if (!project) return null;

  const styles = project.captionStyles || NONE;
  const library = (settings && settings.captionStyles) || NONE;
  const current = resolveStyle(project, { style: sel }, legacy);
  const counts = {};
  for (const c of project.captions || NONE) {
    const id = resolveStyle(project, c, legacy).id || "";
    counts[id] = (counts[id] || 0) + 1;
  }
  const ratio = (project.settings.width || 16) / (project.settings.height || 9);

  const create = ({ id, list }, label) => {
    created.current.add(id);
    setSel(id);
    ops(list, { label }).then((res) => {
      if (!res) created.current.delete(id);
    });
  };
  const edit = (patch, label = "修改字幕樣式", key = null) => {
    if (pending) return;
    if (!sel) return create(editStyleOps(null, patch, legacy), label); // the built-in look becomes a real style
    ops(editStyleOps(sel, patch, legacy).list, { label, coalesce: key && `caption-style:${sel}:${key}` });
  };
  const full = styles.length >= MAX_STYLES;
  const addNew = () => create(addStyleOps(project, { ...DEFAULT_STYLE, size: legacy }, `樣式 ${styles.length + 1}`), "新增字幕樣式");
  const duplicate = () => create(addStyleOps(project, current, `${current.name} 複本`.slice(0, 40)), "複製字幕樣式");
  const addFromLibrary = (lib) => create(addStyleOps(project, lib, lib.name), "加入樣式庫的字幕樣式");
  const addPreset = (preset) => create(addStyleOps(project, preset, preset.name), "加入預設字幕樣式");

  const remove = async () => {
    const n = counts[sel] || 0;
    if (n || sel === defaultId) {
      const ok = await confirmDialog({
        title: "刪除字幕樣式？",
        message: `「${current.name}」${sel === defaultId ? "是專案預設樣式，" : ""}有 ${n} 句字幕使用。刪除後這些字幕改用`
          + `${sel === defaultId ? "內建樣式" : "專案預設樣式"}，可以用復原找回。`,
        confirmLabel: "刪除", danger: true,
      });
      if (!ok) return;
    }
    ops([{ op: "captionStyle.delete", id: sel }], { label: "刪除字幕樣式" });
  };
  const useForAll = async () => {
    const res = await ops(useForAllOps(project, sel), { label: "全部字幕套用同一樣式" });
    if (res) toast(`全部字幕改用「${current.name}」`, { level: "success" });
  };
  const saveToLibrary = async () => {
    const { id: _drop, ...fields } = current;
    const same = library.find((l) => l.name === fields.name);
    if (same) {
      const ok = await confirmDialog({
        title: "取代樣式庫中的樣式？", message: `樣式庫已經有「${fields.name}」，要改成目前的設定嗎？`, confirmLabel: "取代",
      });
      if (!ok) return;
    } else if (library.length >= MAX_STYLES) {
      toast(`樣式庫最多 ${MAX_STYLES} 個樣式，請先刪除用不到的`, { level: "warn" });
      return;
    }
    const next = same ? library.map((l) => (l.id === same.id ? { ...fields, id: same.id } : l))
      : [...library, { ...fields, id: newId("cs") }];
    if (await saveSettings({ captionStyles: next })) toast(`已存到樣式庫：${fields.name}`, { level: "success" });
  };
  const removeFromLibrary = async (lib) => {
    const ok = await confirmDialog({
      title: "從樣式庫刪除？", message: `「${lib.name}」會從樣式庫移除，所有專案都看不到它；已加入專案的樣式不受影響。`,
      confirmLabel: "刪除", danger: true,
    });
    if (ok && await saveSettings({ captionStyles: library.filter((l) => l.id !== lib.id) })) toast(`已從樣式庫刪除：${lib.name}`);
  };

  const footer = html`<span class="modal-foot-note dim"><${Icon} name="info" size=${14} />
      在字幕屬性替每句字幕選擇樣式；在預覽畫面拖曳字幕可以移動位置</span>
    <${Button} icon="save" disabled=${pending} onClick=${saveToLibrary} title="存到樣式庫，其他專案也能使用">存到樣式庫<//>
    <${Button} kind="primary" onClick=${() => close(null)}>完成<//>`;
  return html`<${Modal} title="字幕樣式" width=${980} footer=${footer} backdropClose=${false} onClose=${() => close(null)} class="cs-dialog">
    <div class="cs-layout">
      <aside class="cs-side">
        <div class="cs-side-head">
          <span>這個專案</span>
          <${IconButton} icon="plus" size=${16} title=${full ? `最多 ${MAX_STYLES} 個樣式` : "新增樣式"} disabled=${full} onClick=${addNew} />
        </div>
        <ul class="cs-list">
          ${!defaultId && html`<${StyleItem} s=${{ ...DEFAULT_STYLE, size: legacy, name: "內建樣式" }} active=${!sel}
            isDefault=${true} count=${counts[""] || 0} onSelect=${() => setSel(null)} />`}
          ${styles.map((st) => html`<${StyleItem} key=${st.id} s=${st} active=${st.id === sel} isDefault=${st.id === defaultId}
            count=${counts[st.id] || 0} onSelect=${() => setSel(st.id)} />`)}
        </ul>
        <div class="cs-actions">
          <${Button} size="sm" icon="check" disabled=${pending || sel === defaultId} onClick=${() => ops(setDefaultOps(sel), { label: "設定專案預設字幕樣式" })}
            title="沒有指定樣式的字幕都用這個">設為預設<//>
          <${Button} size="sm" icon="layers" disabled=${pending} onClick=${useForAll}
            title="設為預設，並讓每句字幕都改用這個樣式">全部字幕套用<//>
          <${Button} size="sm" icon="copy" disabled=${pending || full} onClick=${duplicate}>複製<//>
          <${Button} size="sm" icon="trash" kind="danger-ghost" disabled=${pending || !sel} onClick=${remove}>刪除<//>
        </div>
        <div class="cs-side-head cs-lib-head"><span>預設樣式</span><span class="dim">加入後可再調整</span></div>
        <ul class="cs-list">${PRESETS.map((preset) => html`<li key=${preset.name} class="cs-lib-item">
            <span class="cs-swatch" style=${swatchCss(preset)}>字</span>
            <span class="cs-item-name">${preset.name}</span>
            <${IconButton} icon="plus" size=${15} title=${full ? `最多 ${MAX_STYLES} 個樣式` : "加入這個專案"} disabled=${full}
              onClick=${() => addPreset(preset)} />
          </li>`)}</ul>
        <div class="cs-side-head cs-lib-head"><span>樣式庫</span><span class="dim">所有專案共用</span></div>
        ${library.length
          ? html`<ul class="cs-list">${library.map((lib) => html`<li key=${lib.id} class="cs-lib-item">
              <span class="cs-swatch" style=${swatchCss({ ...DEFAULT_STYLE, ...lib })}>字</span>
              <span class="cs-item-name">${lib.name}</span>
              <${IconButton} icon="plus" size=${15} title=${full ? `最多 ${MAX_STYLES} 個樣式` : "加入這個專案"} disabled=${full}
                onClick=${() => addFromLibrary(lib)} />
              <${IconButton} icon="trash" size=${15} title="從樣式庫刪除" onClick=${() => removeFromLibrary(lib)} />
            </li>`)}</ul>`
          : html`<p class="cs-lib-empty dim">按下方「存到樣式庫」，其他專案也能加入這個樣式。</p>`}
        <${Toggle} class="cs-lib-auto" checked=${settings && settings.captionStylesAuto}
          onChange=${(v) => saveSettings({ captionStylesAuto: v })}
          label="新專案自動加入" hint="建立專案時帶入樣式庫全部樣式，第一個設為預設" />
      </aside>
      <${Editor} s=${current} off=${pending} edit=${edit} fonts=${fonts} text=${text || SAMPLE_TEXT} row=${row} ratio=${ratio} />
    </div>
  <//>`;
}

// styleId: the style to open on (else the project default); text and row: the caption shown in the sample.
export function openCaptionStyleDialog({ styleId = null, text = "", row = "bottom" } = {}) {
  return openModal((close) => html`<${StyleBody} close=${close} styleId=${styleId} text=${text} row=${row} />`);
}
