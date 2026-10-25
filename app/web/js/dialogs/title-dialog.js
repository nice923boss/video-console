// Title card templates (GR-10): pick a template, type the words and colours, and it lands at the playhead as
// captions in their own styles (lib/title-templates.js), one undo step. The sample uses the player's caption CSS.
import { html, useState } from "../lib/h.js";
import { getState, setState } from "../lib/store.js";
import { ops } from "../lib/actions.js";
import { clock } from "../lib/clock.js";
import { fmtTime } from "../lib/format.js";
import { textCss, blockCss } from "../lib/caption-style.js";
import { TEMPLATES, lookOf, startValues, titleOps } from "../lib/title-templates.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, TextInput, NumberInput } from "../components/ui.js";
import { toast } from "../components/toast.js";

const cx = (...parts) => parts.filter(Boolean).join(" ");

const Swatch = ({ value, label, onChange }) => html`<label class="route-color cs-color" style=${`--route:${value}`} title=${label}>
  <input type="color" value=${value} onInput=${(e) => onChange(e.currentTarget.value)} /></label>`;

function Sample({ tpl, values, ratio }) {
  return html`<div class="cs-stage"><div class="cs-sample" style=${`aspect-ratio:${ratio}`} title="預覽；實際效果以匯出畫面為準">
    ${tpl.parts.map((p) => {
      const v = values[p.key];
      const text = v.text.trim();
      if (!text) return null;
      const look = lookOf(tpl, p, v);
      return html`<div key=${p.key} class="pl-cap" style=${blockCss(look, { x: look.x, y: look.y })}>
        <span class="pl-cap-text" style=${textCss(look)}>${text}</span></div>`;
    })}
  </div></div>`;
}

function TitleDialog({ close }) {
  const { settings } = getState().project;
  const ratio = (settings.width || 16) / (settings.height || 9);
  const at = clock.time;
  const [tpl, setTpl] = useState(TEMPLATES[0]);
  const [values, setValues] = useState(() => startValues(TEMPLATES[0]));
  const [len, setLen] = useState(TEMPLATES[0].len);
  const [busy, setBusy] = useState(false);

  const pick = (t) => {
    setTpl(t);
    setValues(startValues(t));
    setLen(t.len);
  };
  const edit = (key, patch) => setValues({ ...values, [key]: { ...values[key], ...patch } });

  const add = async () => {
    const res = titleOps(getState().project, tpl, values, at, len);
    if (res.error) {
      toast(res.error, { level: "warn" });
      return;
    }
    setBusy(true);
    const done = await ops(res.list, { label: `加入標題字卡：${tpl.name}` });
    setBusy(false);
    if (!done) return;
    setState({ editSel: { type: "caption", id: res.first } });
    close(true);
    toast(`已加入「${tpl.name}」；改字在字幕屬性，改顏色按「編輯樣式」`, { level: "success" });
  };

  const footer = html`
    <span class="modal-foot-note dim">加在播放頭 ${fmtTime(at)}，可在時間軸字幕列拖曳與調整長度</span>
    <span class="grow"></span>
    <${Button} onClick=${() => close(false)}>取消<//>
    <${Button} kind="primary" icon="type" busy=${busy} onClick=${add}>加入<//>`;

  return html`<${Modal} title="標題字卡" width=${900} footer=${footer} onClose=${() => close(false)} class="cs-dialog">
    <div class="cs-layout">
      <div class="cs-side">
        <div class="cs-side-head">範本</div>
        <ul class="cs-list">${TEMPLATES.map((t) => html`<li key=${t.id}>
          <button type="button" class=${cx("cs-item", t.id === tpl.id && "active")} title=${t.hint} onClick=${() => pick(t)}>
            <span class="cs-item-name">${t.name}</span></button></li>`)}</ul>
        <p class="field-hint">${tpl.hint}。字卡是有自己樣式的字幕，會燒進匯出的影片，但不會寫進 SRT 字幕檔。</p>
      </div>
      <div class="cs-editor">
        <${Sample} tpl=${tpl} values=${values} ratio=${ratio} />
        ${tpl.parts.map((p) => {
          const v = values[p.key];
          const boxed = lookOf(tpl, p).box;
          return html`<section key=${`${tpl.id}-${p.key}`} class="cs-section">
            <header class="cs-section-head"><span class="cs-section-title">${p.label}</span></header>
            <div class="cs-fields">
              <${Field} label="文字" class="cs-span2"><${TextInput} value=${v.text} maxlength=${500} placeholder=${p.text}
                onChange=${(text) => edit(p.key, { text })} /><//>
              <${Field} label="文字顏色"><${Swatch} value=${v.color} label="文字顏色" onChange=${(color) => edit(p.key, { color })} /><//>
              ${boxed && html`<${Field} label="底色"><${Swatch} value=${v.boxColor} label="底色"
                onChange=${(boxColor) => edit(p.key, { boxColor })} /><//>`}
            </div>
          </section>`;
        })}
        <div class="cs-fields">
          <${Field} label="顯示秒數"><${NumberInput} value=${len} min=${1} max=${30} step=${0.5} digits=${1} suffix="秒"
            onCommit=${setLen} /><//>
        </div>
      </div>
    </div>
  <//>`;
}

export const openTitleDialog = () => openModal((close) => html`<${TitleDialog} close=${close} />`);
