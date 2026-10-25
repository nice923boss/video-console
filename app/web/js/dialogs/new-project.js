// "New project" dialog: name, frame shape, size, frame rate, a style preset and optional sample shots.
// Defaults come from Settings > 新專案預設, so the common case is a single click.
import { html, useState } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { createProject } from "../lib/actions.js";
import { isConfirmKey } from "../lib/keymap.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, TextInput, Select, Segmented, Toggle } from "../components/ui.js";
import { Icon } from "../components/icons.js";

const FPS_CHOICES = [24, 25, 29.97, 30];
const NAME_MAX = 120;

function defaultName(projects) {
  const d = new Date();
  const base = `新專案 ${d.getMonth() + 1}/${d.getDate()}`;
  const names = new Set(projects.map((p) => p.name));
  if (!names.has(base)) return base;
  let n = 2;
  while (names.has(`${base}（${n}）`)) n += 1;
  return `${base}（${n}）`;
}

export function AspectOptions({ value, aspects, onPick }) {
  return html`<div class="aspect-pick" role="radiogroup" aria-label="畫面比例">
    ${aspects.map((a) => {
      const [w, h] = a.sizes[0];
      const k = 24 / Math.max(w, h);
      return html`<button type="button" key=${a.id} role="radio" aria-checked=${a.id === value ? "true" : "false"}
          class=${`aspect-opt ${a.id === value ? "active" : ""}`} title=${a.label} onClick=${() => onPick(a)}>
        <span class="aspect-box"><span class="aspect-shape" style=${`width:${Math.round(w * k)}px;height:${Math.round(h * k)}px`}></span></span>
        <span>${a.id}</span>
      </button>`;
    })}
  </div>`;
}

function StyleOptions({ value, styles, onPick }) {
  return html`<div class="style-pick" role="radiogroup" aria-label="風格預設">
    <button type="button" role="radio" aria-checked=${!value ? "true" : "false"}
        class=${`style-opt ${!value ? "active" : ""}`} onClick=${() => onPick("")}>
      <b>不套用</b><span>之後可以在專案設定挑選</span>
    </button>
    ${styles.map((s) => html`<button type="button" key=${s.id} role="radio" aria-checked=${s.id === value ? "true" : "false"}
        class=${`style-opt ${s.id === value ? "active" : ""}`} title=${s.prompt} onClick=${() => onPick(s.id)}>
      <b>${s.label}</b><span>${s.desc}</span>
    </button>`)}
  </div>`;
}

function NewProjectBody({ close, starterDefault }) {
  const s = getState();
  const aspects = (s.presets && s.presets.aspects) || [];
  const styles = (s.presets && s.presets.styles) || [];
  const defaults = (s.settings && s.settings.defaults) || {};
  const firstAspect = aspects.find((a) => a.id === defaults.aspect) || aspects[0] || null;
  const [name, setName] = useState(() => defaultName(s.projects || []));
  const [aspect, setAspect] = useState(firstAspect);
  const [size, setSize] = useState(() => {
    const w = defaults.width || 1920;
    const h = defaults.height || 1080;
    const known = firstAspect && firstAspect.sizes.some(([a, b]) => a === w && b === h);
    return known || !firstAspect ? `${w}x${h}` : firstAspect.sizes[0].join("x");
  });
  const [fps, setFps] = useState(FPS_CHOICES.includes(defaults.fps) ? defaults.fps : 24);
  const [style, setStyle] = useState("");
  const [starter, setStarter] = useState(starterDefault);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const sizes = aspect ? aspect.sizes.map(([w, h], i) => ({
    value: `${w}x${h}`, label: `${w} × ${h}${i === 0 ? "（預設）" : ""}`,
  })) : [{ value: size, label: size.replace("x", " × ") }];
  const pickAspect = (a) => {
    setAspect(a);
    setSize(a.sizes[0].join("x"));
  };

  const submit = async () => {
    const clean = name.trim();
    if (!clean) {
      setError("請輸入專案名稱");
      return;
    }
    setBusy(true);
    const [width, height] = size.split("x").map(Number);
    const preset = styles.find((x) => x.id === style);
    const project = await createProject({
      name: clean.slice(0, NAME_MAX),
      settings: { width, height, fps, aspect: aspect ? aspect.id : defaults.aspect || "16:9" },
      starter,
      style: preset ? { preset: preset.id, prompt: preset.prompt, negative: preset.negative } : null,
    });
    setBusy(false);
    if (project) close(project);
  };

  const footer = html`
    <span class="modal-foot-note dim"><${Icon} name="info" size=${14} />建立後都還能在專案設定修改</span>
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="plus" busy=${busy} onClick=${submit}>建立專案<//>`;

  return html`<${Modal} title="新增專案" width=${640} backdropClose=${false} onClose=${() => close(null)} footer=${footer} class="new-project">
    <${Field} label="專案名稱" error=${error}>
      <${TextInput} value=${name} maxlength=${NAME_MAX} data-autofocus
        onChange=${(v) => { setName(v); if (error) setError(""); }}
        onKeyDown=${(e) => { if ((e.key === "Enter" || isConfirmKey(e)) && !e.isComposing) submit(); }} />
    <//>
    <${Field} label="畫面比例">
      <${AspectOptions} value=${aspect && aspect.id} aspects=${aspects} onPick=${pickAspect} />
    <//>
    <div class="row-2">
      <${Field} label="輸出尺寸" hint="生圖與生影片都用這個尺寸，雲端影片會依供應商支援的比例調整">
        <${Select} value=${size} options=${sizes} onChange=${setSize} />
      <//>
      <${Field} label="影格率">
        <${Segmented} value=${fps} onChange=${setFps}
          options=${FPS_CHOICES.map((v) => ({ value: v, label: `${v} fps` }))} />
      <//>
    </div>
    <${Field} label="風格預設" hint="會帶入風格描述與負向提示詞，套用到每個鏡頭">
      <${StyleOptions} value=${style} styles=${styles} onPick=${setStyle} />
    <//>
    <${Toggle} checked=${starter} onChange=${setStarter} label="加入 3 個範例鏡頭"
      hint="附上示範用的提示詞與運鏡，適合第一次使用時試跑" />
  <//>`;
}

// Resolves with the created project (already opened), or null when canceled.
export function openNewProject({ starter = false } = {}) {
  return openModal((close) => html`<${NewProjectBody} close=${close} starterDefault=${starter} />`);
}
