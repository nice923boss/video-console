// Shot inspector, "生成" tab: which route and engine make the shot's image and video, the template
// parameters, picture prompt, reference image and seed, the image tools (camera move, upscale, video upscale, last frame) and the
// generate buttons docked at the bottom. Every hint mirrors a check the backend makes on submit.
import { html, useState } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { usePref } from "../lib/prefs.js";
import { updateShot, navigate, saveSettings } from "../lib/actions.js";
import { generate } from "../lib/jobs-actions.js";
import { keyHint } from "../lib/keymap.js";
import { randomSeed, round } from "../lib/format.js";
import { imageTake, videoTake } from "../lib/sequence.js";
import { Icon } from "./icons.js";
import { Field, Select, Toggle, Segmented, Slider, NumberInput, Section, Chip, Button, IconButton, Badge, Empty, TextArea,
  useDraft } from "./ui.js";
import { ParamFields } from "./param-fields.js";
import { Thumb } from "./media.js";
import { openRefPicker } from "./ref-picker.js";

const NONE = [];
const NO_ROUTES = [];
const MAX_SEED = 2 ** 53 - 1;
const MAX_REFS = 10; // reference pictures one job may read (backend ops.MAX_REFS)
const COUNT_OPTIONS = [1, 2, 3, 4].map((n) => ({ value: n, label: String(n) }));
const MODE_LABEL = { t2i: "文字生圖", i2i: "圖生圖", t2v: "文字生影片", i2v: "圖生影片" };
const BACKEND_LABEL = { comfy: "ComfyUI", provider: "雲端 API", none: "未開啟" };
const KIND_WORD = { image: "生圖", video: "生影片" };
const goEngines = (tab) => ({ label: "前往設定", fn: () => navigate("engines", { enginesTab: tab }) });

// The Ken Burns preset that matches the shot's camera move, so the animation pushes the way the shot says.
export function kenburnsFor(shot, presets) {
  const cam = (shot.camera || "").trim();
  const m = ((presets && presets.cameraMoves) || NONE).find((x) => x.text === cam || x.en === cam);
  return (m && m.kenburns) || "zoom_in";
}

// value undefined (a multi-selection that disagrees) highlights nothing.
export function RouteChips({ routes, value, onChange }) {
  return html`<div class="chips">
    ${routes.map((r) => html`<${Chip} key=${r.id} active=${value === r.id} title=${`路由 ${r.id}：${r.name}`}
        onClick=${() => value !== r.id && onChange(r.id)}>
      <i class="route-dot" style=${`--route:${r.color || "var(--muted)"}`}></i>
      <b class="mono">${r.id}</b><span>${r.name}</span>
    <//>`)}
  </div>`;
}

function Note({ tone = "info", action = null, children }) {
  return html`<div class=${`note note-${tone}`}>
    <${Icon} name=${tone === "warn" ? "alert" : "info"} size=${14} />
    <span class="note-text">${children}</span>
    ${action && html`<${Button} size="sm" kind="ghost" onClick=${action.fn}>${action.label}<//>`}
  </div>`;
}

// What a route section resolves to for this shot, in the order the backend picks it.
function resolveEngine(kind, section, shot, workflows, providers, hasRef) {
  const backend = section && BACKEND_LABEL[section.backend] ? section.backend : "none";
  if (backend === "comfy") {
    const own = shot[`${kind}Workflow`] || "";
    const routeWid = (kind === "image" && hasRef && section.workflowI2I) || section.workflow || "";
    const wid = own || routeWid;
    const tpl = workflows.find((w) => w.id === wid) || null;
    return { backend, own, routeWid, wid, tpl, label: tpl ? tpl.name : wid || "未指定範本" };
  }
  if (backend === "provider") {
    const prov = providers.find((p) => p.id === section.provider) || null;
    return { backend, pid: section.provider || "", prov, label: prov ? prov.name : section.provider || "未指定供應商" };
  }
  return { backend, label: "" };
}

function RouteSummary({ image, video }) {
  const line = (icon, word, e) => html`<div class="route-line">
    <${Icon} name=${icon} size=${14} /><span>${word}</span><${Icon} name="arrow-right" size=${12} />
    <span class=${e.backend === "none" ? "dim" : ""}>${BACKEND_LABEL[e.backend]}${e.label ? ` · ${e.label}` : ""}</span>
  </div>`;
  return html`<div class="route-summary">${line("image", "生圖", image)}${line("film", "影片", video)}</div>`;
}

// The frame size the backend sends to a template with a short-side cap (snapped to 32 like video
// models need), or null when the project size already fits.
function cappedSize(tpl, settings) {
  const cap = tpl.maxShortSide || 0;
  const short = Math.min(settings.width, settings.height);
  if (!cap || short <= cap) return null;
  const snap = (v) => Math.max(64, Math.round((v * cap) / short / 32) * 32);
  return [snap(settings.width), snap(settings.height)];
}

function comfyNotes(kind, tpl, shot, hasSource, settings) {
  const notes = [];
  const name = tpl.name;
  if (kind === "image" && tpl.mode === "i2i" && !hasSource) notes.push(["warn", `範本「${name}」需要參考圖，請在下方設定參考圖`]);
  if (kind === "image" && tpl.mode === "t2i" && hasSource) notes.push(["info", `範本「${name}」不使用參考圖，生圖時會略過`]);
  if (kind === "video" && tpl.mode === "i2v" && !hasSource) notes.push(["warn", `範本「${name}」需要鏡頭圖片，請先生圖或匯入圖片`]);
  if (kind === "video" && tpl.mode === "t2v" && hasSource) notes.push(["info", `範本「${name}」不使用鏡頭圖片，會改用文字生成`]);
  if (kind === "video" && tpl.maxFrames) {
    const cap = tpl.maxFrames / (tpl.defaultFps || settings.fps || 24);
    const want = shot.videoDuration || shot.duration;
    if (want > cap + 0.01) {
      notes.push(["info", `這個範本一次最多 ${round(cap, 1)} 秒，影片會比${shot.videoDuration ? "指定的秒數" : "鏡頭長度"}（${round(want, 1)} 秒）短，可在剪輯台調整`]);
    }
  }
  const size = kind === "video" && cappedSize(tpl, settings);
  if (size) {
    notes.push(["info", `這個範本以 ${size[0]}×${size[1]} 生成（短邊上限 ${tpl.maxShortSide}），匯出時會放大成專案的 ${settings.width}×${settings.height}`]);
  }
  if (!tpl.verified) notes.push(["info", "這個範本還沒在本機實測過，需要的模型請看「引擎 › 範本」的說明"]);
  return notes;
}

function ComfyBlock({ kind, route, engine, shot, workflows, hasSource, settings }) {
  const field = `${kind}Workflow`;
  const pfield = `${kind}Params`;
  const routeTpl = workflows.find((w) => w.id === engine.routeWid);
  const options = [
    { value: "", label: `依路由預設（${routeTpl ? routeTpl.name : engine.routeWid || "未指定"}）` },
    ...workflows.filter((w) => w.kind === kind).map((w) => ({ value: w.id, label: w.verified ? w.name : `${w.name}（未驗證）` })),
  ];
  const tpl = engine.tpl;
  const notes = tpl ? comfyNotes(kind, tpl, shot, hasSource, settings) : [];
  return html`
    <${Field} label="ComfyUI 範本" hint=${(tpl && tpl.description) || ""}>
      <${Select} value=${engine.own} options=${options}
        onChange=${(v) => updateShot(shot.id, { [field]: v }, { label: "變更生成範本" })} />
    <//>
    ${!engine.wid && html`<${Note} tone="warn" action=${goEngines("routes")}>路由「${route.name}」還沒有指定 ComfyUI 範本<//>`}
    ${engine.wid && !tpl && html`<${Note} tone="warn">找不到範本「${engine.wid}」，可能已被刪除，請改選其他範本<//>`}
    ${notes.map(([tone, text]) => html`<${Note} key=${text} tone=${tone}>${text}<//>`)}
    ${tpl && html`<${ParamFields} params=${tpl.params} values=${shot[pfield]}
      onChange=${(v) => updateShot(shot.id, { [pfield]: v }, { label: "修改生成參數", coalesce: `${shot.id}:${pfield}` })} />`}`;
}

function ProviderBlock({ kind, route, engine, hasSource }) {
  const secrets = useStore((s) => s.secrets);
  const p = engine.prov;
  if (!engine.pid) return html`<${Note} tone="warn" action=${goEngines("routes")}>路由「${route.name}」還沒有指定雲端供應商<//>`;
  if (!p) return html`<${Note} tone="warn" action=${goEngines("routes")}>找不到雲端供應商「${engine.pid}」<//>`;
  const modes = p.modes || NONE;
  const [txt, img] = kind === "image" ? ["t2i", "i2i"] : ["t2v", "i2v"];
  const names = p.secretNames || {};
  const missing = p.id.startsWith("custom-") ? NONE
    : (p.secrets || NONE).filter((s) => !((secrets || {})[names[s.key]] || {}).set);
  const what = kind === "image" ? "參考圖" : "鏡頭圖片";
  return html`
    <div class="prov-card">
      <div class="prov-name"><${Icon} name="cloud" size=${15} /><b>${p.name}</b>${p.vendor && html`<span class="dim">${p.vendor}</span>`}</div>
      <div class="prov-modes">${modes.map((m) => html`<${Badge} key=${m} tone=${m === img || m === txt ? "blue" : "neutral"}>${MODE_LABEL[m] || m}<//>`)}</div>
    </div>
    ${missing.length > 0 && html`<${Note} tone="warn" action=${{ label: "前往填寫", fn: () => navigate("engines", { enginesTab: "providers", providerSel: p.id }) }}>
      尚未填寫 ${missing.map((s) => s.label).join("、")}<//>`}
    ${hasSource && !modes.includes(img) && modes.includes(txt) && html`<${Note}>
      ${p.name} 不支援${kind === "image" ? "參考圖，生圖時會略過" : "圖生影片，會改用文字生成"}<//>`}
    ${!hasSource && !modes.includes(txt) && html`<${Note} tone="warn">${p.name} 需要${what}才能生成<//>`}`;
}

function EngineBlock(props) {
  const { kind, route, engine } = props;
  if (engine.backend === "comfy") return html`<${ComfyBlock} ...${props} />`;
  if (engine.backend === "provider") return html`<${ProviderBlock} ...${props} />`;
  return html`<${Note} tone="warn" action=${goEngines("routes")}>
    路由「${route.name}」沒有開啟${KIND_WORD[kind]}，可以換一條路由或到「引擎」頁設定<//>`;
}

// What a picture job sends when the shot has no prompt of its own (runners.py _prompts, sequence.compose_prompt).
function composedImagePrompt(project, shot) {
  return [shot.prompt, (project.style && project.style.prompt) || ""]
    .map((p) => (p || "").trim().replace(/^[,，]+|[,，]+$/g, ""))
    .filter(Boolean)
    .join("\n");
}

// The picture prompt goes out exactly as written; an empty box is filled with the text each submit sent
// (jobs-actions.js keepImagePrompts), so the box always shows what made the picture.
function ImagePrompt({ project, shot }) {
  const auto = composedImagePrompt(project, shot);
  const [text, setText, flush] = useDraft(shot.imagePrompt || "",
    (v) => updateShot(shot.id, { imagePrompt: v }, { coalesce: `${shot.id}:imagePrompt`, label: "修改生圖提示詞" }));
  const rebuild = () => {
    setText(auto);
    flush();
  };
  const hint = text.trim()
    ? "生圖時照框內文字原樣送出；改了鏡頭描述或全片風格後，按「重組」更新"
    : "留空時用鏡頭描述加全片風格組成，送出後會把實際送出的文字寫進來";
  return html`<${Field} label="生圖提示詞" hint=${hint}
      after=${html`<span class="char-count mono" title="字數">${text.length}</span>
        <${Button} size="sm" kind="ghost" icon="refresh" disabled=${!auto || auto === text}
          title="依鏡頭描述和全片風格重新組成" onClick=${rebuild}>重組<//>`}>
    <${TextArea} value=${text} onChange=${setText} onBlur=${flush} rows=${3} autosize maxLength=${8000}
      placeholder=${auto || "還沒有鏡頭描述，可以直接在這裡寫生圖提示詞"} />
  <//>`;
}

// The shot's extra reference pictures (pictures 2 to MAX_REFS) for templates that read several at once.
function ExtraRefs({ project, shot, item, ratio }) {
  const extras = shot.extraRefs || NONE;
  const find = (id) => project.takes[id] || project.media[id] || null;
  const add = async () => {
    const taken = [shot.ref, ...extras];
    const id = await openRefPicker(project, shot, taken);
    if (id && !taken.includes(id)) updateShot(shot.id, { extraRefs: [...extras, id] }, { label: "新增參考圖" });
  };
  const drop = (id) => updateShot(shot.id, { extraRefs: extras.filter((x) => x !== id) }, { label: "移除參考圖" });
  return html`<div class="ref-extras">
    <div class="ref-extra">
      <${Thumb} pid=${project.id} path=${item.thumb || item.file} ratio=${ratio} />
      <span class="ref-cell-label">第 1 張</span>
    </div>
    ${extras.map((id, i) => {
      const x = find(id);
      return x && html`<div class="ref-extra" key=${id}>
        <${Thumb} pid=${project.id} path=${x.thumb || x.file} ratio=${ratio} />
        <span class="ref-cell-label">第 ${i + 2} 張</span>
        <${IconButton} icon="x" size=${13} class="ref-extra-x" title="移除這張參考圖" onClick=${() => drop(id)} />
      </div>`;
    })}
    ${extras.length < MAX_REFS - 1 && html`<button type="button" class="ref-empty ref-extra-add" style=${`aspect-ratio: ${ratio}`}
      onClick=${add}>
      <${Icon} name="plus" size=${16} /><span>新增</span>
    </button>`}
  </div>`;
}

// tpl: the ComfyUI template that will make the picture, or null for a cloud provider. It decides whether
// the strength slider applies (Klein redraws the whole frame, no denoise) and whether extra pictures are read.
function RefField({ project, shot, tpl }) {
  const item = shot.ref ? project.takes[shot.ref] || project.media[shot.ref] || null : null;
  const ratio = `${project.settings.width}/${project.settings.height}`;
  const multi = !!(tpl && tpl.bindings.images);
  const strength = !tpl || !!tpl.bindings.denoise;
  const extras = shot.extraRefs || NONE;
  const pick = async () => {
    const id = await openRefPicker(project, shot);
    if (id && id !== shot.ref) {
      updateShot(shot.id, { ref: id, extraRefs: extras.filter((x) => x !== id) }, { label: "設定參考圖" });
    }
  };
  // Removing the first picture moves the next one up, so the extra pictures are not left orphaned.
  const remove = () => updateShot(shot.id, { ref: extras[0] || null, extraRefs: extras.slice(1) }, { label: "移除參考圖" });
  if (shot.ref && !item) {
    return html`<${Field} label="參考圖"><${Note} tone="warn" action=${{ label: "移除", fn: remove }}>參考圖已被刪除<//><//>`;
  }
  const hint = multi ? `最多 ${MAX_REFS} 張；提示詞用「第一張圖的人物、第二張圖的場景」指名`
    : strength ? "生圖時參考這張圖的構圖與色調；強度越高越接近原圖"
    : "這個範本整張重畫，要保留或修改什麼請寫進提示詞";
  return html`<${Field} label=${multi ? `參考圖（${item ? 1 + extras.length : 0}/${MAX_REFS}）` : "參考圖"} hint=${hint}>
    ${item ? html`<div class="ref-pick">
        <${Thumb} pid=${project.id} path=${item.thumb || item.file} ratio=${ratio} />
        <div class="ref-side">
          <${Button} size="sm" icon="images" onClick=${pick}>更換<//>
          <${Button} size="sm" kind="ghost" icon="x" onClick=${remove}>移除<//>
        </div>
      </div>
      ${multi && html`<${ExtraRefs} project=${project} shot=${shot} item=${item} ratio=${ratio} />`}
      ${!multi && extras.length > 0 && html`<${Note}>這個範本只讀第 1 張參考圖，另外 ${extras.length} 張會略過<//>`}
      ${strength && html`<div class="ref-strength">
        <span class="field-label">參考強度</span>
        <${Slider} value=${shot.refStrength ?? 0.6} min=${0.05} max=${1} step=${0.05} format=${(v) => `${Math.round(v * 100)}%`}
          onCommit=${(v) => updateShot(shot.id, { refStrength: round(v, 2) }, { label: "修改參考強度" })} />
      </div>`}`
    : html`<button type="button" class="ref-empty" onClick=${pick}>
        <${Icon} name="image" size=${18} /><span>選擇參考圖</span>
      </button>`}
  <//>`;
}

function VideoSource({ project, shot, img, useImage }) {
  if (!img) return html`<p class="insp-hint">這個鏡頭還沒有圖片，影片會用文字生成；先生圖可以讓畫面更好掌控</p>`;
  return html`<${Field} label="起始畫面">
    <div class="ref-pick">
      <${Thumb} pid=${project.id} path=${img.thumb || img.file} ratio=${`${project.settings.width}/${project.settings.height}`} />
      <div class="ref-side">
        <${Toggle} checked=${useImage} label="用鏡頭圖片當起始畫面"
          onChange=${(videoUseImage) => updateShot(shot.id, { videoUseImage }, { label: "切換起始畫面" })} />
      </div>
    </div>
  <//>`;
}

// The video lengths the provider's model accepts for the mode the backend will use; "自動" picks the one
// nearest the shot length (a tie goes to the shorter one), like the backend does on submit.
function VideoDuration({ shot, prov, hasStart }) {
  const modes = prov.modes || NONE;
  const mode = (hasStart && modes.includes("i2v")) || !modes.includes("t2v") ? "i2v" : "t2v";
  const list = (prov.durations || {})[mode];
  if (list == null) return null;
  if (!list.length) return html`<${Note}>這個模型不能指定秒數，影片長度由模型決定<//>`;
  const sorted = [...list].sort((a, b) => a - b);
  const len = shot.duration || 0;
  const nearest = sorted.reduce((a, b) => (Math.abs(b - len) < Math.abs(a - len) ? b : a));
  const options = [
    { value: null, label: `自動（最接近鏡頭長度 ${round(len, 1)} 秒：${nearest} 秒）` },
    ...sorted.map((s) => ({ value: s, label: `${s} 秒` })),
  ];
  const value = shot.videoDuration ?? null;
  const missing = value != null && !sorted.includes(value);
  return html`<${Field} label="影片秒數" hint="只列出這個模型支援的秒數">
    <${Select} value=${missing ? undefined : value} options=${options} placeholder=${`${value} 秒（這個模型沒有）`}
      onChange=${(v) => updateShot(shot.id, { videoDuration: v }, { label: "修改影片秒數" })} />
    ${missing && html`<${Note} tone="warn">這個模型沒有 ${value} 秒，生成時會改用最接近的秒數<//>`}
  <//>`;
}

// ComfyUI video length in whole seconds up to the template's frame cap; "自動" follows the shot length.
// The backend snaps the frame count to the model's rule (8n+1 for LTX-Video), so a clip can end up a
// few frames longer or shorter than the seconds picked here.
function ComfyVideoDuration({ shot, tpl, fps }) {
  const rate = tpl.defaultFps || fps || 24;
  const most = tpl.maxFrames ? Math.floor(tpl.maxFrames / rate) : 10;
  const options = [
    { value: null, label: `自動（跟鏡頭長度 ${round(shot.duration || 0, 1)} 秒）` },
    ...Array.from({ length: most }, (_, i) => ({ value: i + 1, label: `${i + 1} 秒` })),
  ];
  const value = shot.videoDuration ?? null;
  const listed = options.some((o) => o.value === value);
  const hint = tpl.maxFrames ? `這個範本最多 ${round(tpl.maxFrames / rate, 1)} 秒（${tpl.maxFrames} 格，${rate} fps）` : "";
  return html`<${Field} label="影片秒數" hint=${hint}>
    <${Select} value=${listed ? value : undefined} options=${options} placeholder=${`${value} 秒`}
      onChange=${(v) => updateShot(shot.id, { videoDuration: v }, { label: "修改影片秒數" })} />
  <//>`;
}

function SeedSection({ shot }) {
  const locked = !!shot.seedLocked;
  return html`<${Section} title="種子" icon="dice">
    <${Toggle} checked=${locked} label="鎖定種子" hint="鎖定後每次都用同一個種子，方便只改描述做比較"
      onChange=${(seedLocked) => updateShot(shot.id, { seedLocked }, { label: seedLocked ? "鎖定種子" : "解除鎖定種子" })} />
    <div class="row mt-6">
      <${NumberInput} value=${shot.seed || 0} min=${0} max=${MAX_SEED} step=${1} digits=${0} disabled=${!locked} class="grow"
        title="種子" onCommit=${(seed) => updateShot(shot.id, { seed: Math.round(seed) }, { label: "修改種子" })} />
      <${IconButton} icon="dice" title="換一個隨機種子並鎖定"
        onClick=${() => updateShot(shot.id, { seed: randomSeed(), seedLocked: true }, { label: "隨機種子" })} />
    </div>
    <p class="insp-hint">${locked ? "生圖與影片都使用這個種子；一次生成多份時會依序加 1"
      : "目前每次隨機；生圖完成後會記下這次的種子，之後鎖定就能重現"}</p>
  <//>`;
}

const UPSCALE_OPTIONS = [
  { value: "anime4k", label: "快速 Anime4K", title: "5 秒片段約 4 秒，邊緣銳利但細紋會被抹平" },
  { value: "realesrgan", label: "細節 Real-ESRGAN", title: "5 秒片段約 5 到 6 分鐘，保留較多細節" },
];

// The short side a video needs to be below for Video2X to have anything to do (video2x.SHORT_SIDE).
const FULL_HD = 1080;

function ToolsSection({ project, shot, index, img, vid }) {
  const presets = useStore((s) => s.presets);
  const workflows = useStore((s) => s.workflows || NONE);
  const defaultUp = useStore((s) => (s.settings && s.settings.comfy && s.settings.comfy.upscaleWorkflow) || "upscale-ultrasharp");
  const [kb, setKb] = useState(() => kenburnsFor(shot, presets));
  const [upWid, setUpWid] = useState(defaultUp);
  const [upParams, setUpParams] = useState({});
  const isLast = index + 1 >= project.shots.length;
  const [target, setTarget] = useState(isLast ? "self" : "next");
  const upTpl = workflows.find((w) => w.id === upWid) || null;
  const upOptions = workflows.filter((w) => w.kind === "upscale").map((w) => ({ value: w.id, label: w.name }));
  const noImage = img ? "" : "需要鏡頭圖片";
  const method = useStore((s) => (s.settings && s.settings.video2x && s.settings.video2x.method) || "anime4k");
  const bigEnough = !!vid && Math.min(vid.width || 0, vid.height || 0) >= FULL_HD;
  const noVideo = !vid ? "需要鏡頭影片" : bigEnough ? `影片已是 ${vid.width}×${vid.height}，不需要放大` : "";
  return html`<${Section} title="圖片與影片工具" icon="wand" collapsible id="insp-tools">
    <div class="tool">
      <div class="tool-head"><${Icon} name="move" size=${15} /><b>運鏡動畫</b><span>用鏡頭圖片做推拉搖移，不需要 AI 模型</span></div>
      <div class="row">
        <${Select} class="grow" value=${kb} options=${(presets.kenburns || NONE).map((k) => ({ value: k.id, label: k.label }))} onChange=${setKb} />
        <${Button} icon="play" disabled=${!img} title=${noImage || "用 ffmpeg 製作運鏡影片"}
          onClick=${() => generate("kenburns", [shot.id], { payload: { preset: kb, duration: shot.duration } })}>製作<//>
      </div>
    </div>
    <div class="tool">
      <div class="tool-head"><${Icon} name="zoom-in" size=${15} /><b>放大</b><span>用 ComfyUI 把鏡頭圖片放大，結果存成新版本</span></div>
      <div class="row">
        <${Select} class="grow" value=${upWid} options=${upOptions} placeholder="沒有放大範本"
          onChange=${(v) => { setUpWid(v); setUpParams({}); }} />
        <${Button} icon="zoom-in" disabled=${!img || !upTpl} title=${noImage || (upTpl ? "送出放大工作" : "找不到放大範本")}
          onClick=${() => generate("upscale", [shot.id], { payload: { workflow: upWid, params: upParams } })}>放大<//>
      </div>
      ${upTpl && html`<${ParamFields} params=${upTpl.params} values=${upParams} onChange=${setUpParams} />`}
    </div>
    <div class="tool">
      <div class="tool-head"><${Icon} name="film" size=${15} /><b>影片放大</b><span>用 Video2X 把鏡頭影片放大到 1080P，結果存成新版本</span></div>
      <div class="row">
        <${Segmented} size="sm" value=${method} onChange=${(m) => saveSettings({ video2x: { method: m } })}
          options=${UPSCALE_OPTIONS} />
        <span class="grow"></span>
        <${Button} icon="zoom-in" disabled=${!!noVideo} title=${noVideo || "送出 Video2X 放大工作"}
          onClick=${() => generate("upscalevideo", [shot.id], { payload: { method } })}>放大<//>
      </div>
    </div>
    <div class="tool">
      <div class="tool-head"><${Icon} name="frame" size=${15} /><b>取末格</b><span>把影片最後一格存成圖片，讓鏡頭之間畫面接得上</span></div>
      <div class="row">
        <${Segmented} size="sm" value=${target} onChange=${setTarget} options=${[
          { value: "next", label: "接到下一鏡", disabled: isLast, title: isLast ? "這是最後一個鏡頭" : "" },
          { value: "self", label: "這個鏡頭" },
        ]} />
        <span class="grow"></span>
        <${Button} icon="frame" disabled=${!vid || (target === "next" && isLast)} title=${vid ? "擷取影片最後一格" : "需要鏡頭影片"}
          onClick=${() => generate("lastframe", [shot.id], { payload: { target } })}>擷取<//>
      </div>
    </div>
  <//>`;
}

function GenDock({ shot, busy, image, video, useImage }) {
  const [count, setCount] = usePref("vc.gen.count", 1, (v) => [1, 2, 3, 4].includes(v));
  const [chain, setChain] = usePref("vc.gen.chain", false, (v) => typeof v === "boolean");
  const { upscale, method } = useStore((s) => {
    const v2x = (s.settings && s.settings.video2x) || {};
    return { upscale: !!v2x.enabled, method: v2x.method || "anime4k" };
  });
  const times = count > 1 ? ` ×${count}` : "";
  const offImage = image.backend === "none";
  const offVideo = video.backend === "none";
  return html`<div class="gen-dock">
    <div class="row">
      <span class="field-label">份數</span>
      <${Segmented} size="sm" value=${count} onChange=${setCount} options=${COUNT_OPTIONS} />
      <span class="grow"></span>
      ${busy.length > 0 && html`<${Badge} tone="accent">${busy.length} 個工作進行中<//>`}
    </div>
    <${Toggle} checked=${chain} onChange=${setChain} label="生圖完成後接著生影片" disabled=${offVideo} />
    <${Toggle} checked=${upscale} onChange=${(enabled) => saveSettings({ video2x: { enabled } })}
      label="生影片後用 Video2X 放大到 1080P" hint="限 ComfyUI 生成的影片，放大完才放上時間軸"
      disabled=${video.backend !== "comfy"} />
    ${upscale && video.backend === "comfy" && html`<div class="row">
      <span class="field-label">放大方式</span>
      <${Segmented} size="sm" value=${method} onChange=${(m) => saveSettings({ video2x: { method: m } })}
        options=${UPSCALE_OPTIONS} />
    </div>`}
    <div class="row gen-actions">
      <${Button} kind="primary" icon="sparkles" class="grow" disabled=${offImage} title=${offImage ? "這條路由沒有開啟生圖" : `生成圖片${keyHint("board.image")}`}
        onClick=${() => generate("image", [shot.id], { count, payload: chain && !offVideo ? { chainVideo: true } : {} })}>生圖${times}<//>
      <${Button} icon="film" class="grow" disabled=${offVideo} title=${offVideo ? "這條路由沒有開啟生影片" : `生成影片${keyHint("board.video")}`}
        onClick=${() => generate("video", [shot.id], { count, payload: useImage ? {} : { useImage: false } })}>生影片${times}<//>
    </div>
  </div>`;
}

export function GenerateTab({ project, shot, index, busy }) {
  const { routes, workflows, providers } = useStore((s) => ({
    routes: (s.settings && s.settings.routes) || NO_ROUTES, workflows: s.workflows || NONE, providers: s.providers || NONE,
  }));
  const route = routes.find((r) => r.id === shot.route) || routes[0] || null;
  if (!route) {
    return html`<${Empty} icon="route" title="還沒有任何路由"
      actions=${html`<${Button} kind="primary" onClick=${goEngines("routes").fn}>前往設定<//>`}>路由決定生圖與影片要交給哪個引擎<//>`;
  }
  const img = imageTake(project, shot);
  const vid = videoTake(project, shot);
  const hasRef = !!shot.ref;
  const useImage = shot.videoUseImage !== false;
  const hasStart = useImage && !!img;
  const image = resolveEngine("image", route.image, shot, workflows, providers, hasRef);
  const video = resolveEngine("video", route.video, shot, workflows, providers, false);
  const common = { route, shot, workflows, settings: project.settings };
  return html`
    <${Section} title="路由" icon="route">
      <${RouteChips} routes=${routes} value=${shot.route}
        onChange=${(r) => updateShot(shot.id, { route: r }, { label: "變更路由" })} />
      ${route.id !== shot.route && html`<${Note} tone="warn">路由「${shot.route}」已不存在，生成時會改用「${route.name}」<//>`}
      <${RouteSummary} image=${image} video=${video} />
    <//>
    <${Section} title="生圖設定" icon="image" actions=${html`<${Badge}>${BACKEND_LABEL[image.backend]}<//>`}>
      <${EngineBlock} kind="image" engine=${image} hasSource=${hasRef} ...${common} />
      ${image.backend !== "none" && html`<${ImagePrompt} project=${project} shot=${shot} />`}
      ${image.backend !== "none" && html`<${RefField} project=${project} shot=${shot}
        tpl=${image.backend === "comfy" ? image.tpl : null} />`}
    <//>
    <${Section} title="影片設定" icon="film" actions=${html`<${Badge}>${BACKEND_LABEL[video.backend]}<//>`}>
      <${EngineBlock} kind="video" engine=${video} hasSource=${hasStart} ...${common} />
      ${video.backend !== "none" && html`<${VideoSource} project=${project} shot=${shot} img=${img}
        useImage=${useImage} />`}
      ${video.backend === "provider" && video.prov && html`<${VideoDuration} shot=${shot} prov=${video.prov} hasStart=${hasStart} />`}
      ${video.backend === "comfy" && video.tpl && html`<${ComfyVideoDuration} shot=${shot} tpl=${video.tpl} fps=${project.settings.fps} />`}
    <//>
    <${SeedSection} shot=${shot} />
    <${ToolsSection} project=${project} shot=${shot} index=${index} img=${img} vid=${vid} />
    <${GenDock} shot=${shot} busy=${busy || NONE} image=${image} video=${video} useImage=${useImage} />`;
}
