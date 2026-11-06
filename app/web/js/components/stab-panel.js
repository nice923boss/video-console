// Stabilize settings (edits.py stabilize with settings, media.py STAB_DEFAULTS): a draft the user tunes, then
// "套用" renders a copy with exactly these settings from the clip's footage (or switches to one made before).
// engine "gyro" makes the copy with Gyroflow from the camera original (gyro.py), found by file name in the
// project's camera folder (settings.gyroFolder).
import { html, useState, useEffect } from "../lib/h.js";
import { Field, NumberInput, Select, Toggle, Slider, Button, DraftText } from "./ui.js";
import { round } from "../lib/format.js";
import { useStore } from "../lib/store.js";
import { ops, pickSystemFolder } from "../lib/actions.js";

// Mirrors media.py STAB_DEFAULTS; a copy made before settings existed used exactly these.
export const STAB_DEFAULTS = Object.freeze({
  shakiness: 5, accuracy: 15, stepsize: 6, mincontrast: 0.25, smoothing: 15, zoom: 0, zoomspeed: 0.25,
  maxshift: -1, maxangle: -1, sharpen: 0.8, optzoom: 1, crop: "keep", optalgo: "opt", interpol: "bilinear",
  tripod: false, relative: true, engine: "vidstab", gyroSmooth: 0.5,
});
const ENGINES = [{ value: "vidstab", label: "畫面分析（vidstab）" }, { value: "gyro", label: "陀螺儀（Gyroflow）" }];
// Mirrors media.py GYRO_KEYS: all a gyro copy is made with; the other keys go back to defaults (clean_stab).
const GYRO_KEYS = ["engine", "gyroSmooth"];
const normal = (d) => Object.fromEntries(Object.entries(d).map(([k, v]) =>
  [k, k === "engine" || GYRO_KEYS.includes(k) === (d.engine === "gyro") ? v : STAB_DEFAULTS[k]]));

const pct = (v) => `${round(v, 2)}%`;
const MAIN = [
  { k: "shakiness", label: "晃動程度", min: 1, max: 10, step: 1, hint: "畫面晃得越厲害、鏡頭動得越快就調高（分析比較慢）" },
  { k: "smoothing", label: "平滑度", min: 0, max: 120, step: 1, format: (v) => `${v} 格`,
    hint: "前後各取幾格平均鏡頭路徑：越高越穩，但跟拍轉向會比較慢跟上；0 等於鎖住不動" },
  { k: "zoom", label: "額外放大", min: -20, max: 30, step: 1, format: pct, hint: "在自動放大之外再放大，負值縮小" },
  { k: "sharpen", label: "銳利化", min: 0, max: 2, step: 0.1, format: (v) => round(v, 2),
    hint: "防抖會讓畫面略軟，補一點銳利；0 不處理" },
];
const ADVANCED = [
  { k: "accuracy", label: "分析精度", min: 1, max: 15, step: 1, hint: "不會低於晃動程度" },
  { k: "stepsize", label: "搜尋步距", min: 1, max: 32, step: 1, hint: "像素；越小越準越慢" },
  { k: "mincontrast", label: "最低對比", min: 0, max: 1, step: 0.05, hint: "對比低於這個值的區塊不拿來追蹤" },
  { k: "maxshift", label: "最大位移", min: -1, max: 500, step: 1, hint: "像素，-1 不限" },
  { k: "maxangle", label: "最大旋轉", min: -1, max: 3.14, step: 0.01, hint: "弧度，-1 不限" },
  { k: "zoomspeed", label: "動態放大速度", min: 0, max: 5, step: 0.05, hint: "邊緣處理選「動態放大」時，每格最多放大的百分比" },
];
const OPTZOOM = [
  { value: 1, label: "放大到看不到黑邊（固定倍率）" },
  { value: 2, label: "跟著晃動動態放大" },
  { value: 0, label: "不自動放大" },
];
const CROP = [{ value: "keep", label: "沿用前一格" }, { value: "black", label: "黑色" }];
const OPTALGO = [{ value: "opt", label: "整段最佳化" }, { value: "gauss", label: "高斯平均" }, { value: "avg", label: "簡單平均" }];
const INTERPOL = [{ value: "bicubic", label: "雙立方（最清晰）" }, { value: "bilinear", label: "雙線性" },
  { value: "linear", label: "水平線性" }, { value: "no", label: "不內插（最快）" }];

const same = (a, b) => Object.keys(STAB_DEFAULTS).every((k) => a[k] === b[k]);

// The project's camera folder, where gyro stabilization looks up the originals by file name.
function GyroFolder() {
  const folder = useStore((s) => (s.project && s.project.settings && s.project.settings.gyroFolder) || "");
  const save = (v) => {
    const next = String(v || "").trim();
    if (next !== folder) ops([{ op: "project.update", patch: { settings: { gyroFolder: next } } }], { label: "修改相機原始檔資料夾", coalesce: "settings:gyroFolder" });
  };
  const pick = async () => {
    const path = await pickSystemFolder(folder || null, "選擇相機原始檔資料夾");
    if (path) save(path);
  };
  return html`<${Field} label="相機原始檔資料夾"
    hint="記憶卡複製出來的資料夾（含 .MXF／.MP4）；依匯入時的檔名找同名原始檔，往下找 3 層，並核對影格率與長度">
    <div class="row">
      <${DraftText} class="grow" value=${folder} onCommit=${save} placeholder="例如 F:/拍攝/FX6" />
      <${Button} size="sm" icon="folder-open" onClick=${pick}>選資料夾…<//>
    </div>
  <//>`;
}

// current: the settings the clip's copy was made with (null when the clip is not stabilized).
export function StabilizeSettings({ current, disabled, onApply }) {
  const base = { ...STAB_DEFAULTS, ...(current || {}) };
  const key = JSON.stringify(base);
  const [draft, setDraft] = useState(base);
  useEffect(() => setDraft(base), [key]);
  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const slider = (f) => html`<${Field} key=${f.k} label=${f.label} hint=${f.hint}>
    <${Slider} value=${draft[f.k]} min=${f.min} max=${f.max} step=${f.step} format=${f.format}
      onChange=${(v) => set(f.k, v)} onCommit=${(v) => set(f.k, v)} />
  <//>`;
  const num = (f) => html`<${Field} key=${f.k} label=${f.label} hint=${f.hint}>
    <${NumberInput} value=${draft[f.k]} min=${f.min} max=${f.max} step=${f.step} onCommit=${(v) => set(f.k, v)} />
  <//>`;
  const gyro = draft.engine === "gyro";
  const applied = current && same(normal(draft), normal(base));
  return html`<details class="grade-sec">
    <summary>防抖設定${current && !same(base, STAB_DEFAULTS) && html`<span class="grade-dot" title="不是預設值"></span>`}</summary>
    <div class="grade-sec-body">
      <${Field} label="防抖方式" hint=${gyro
        ? "讀相機原始檔記下的晃動來修正（Sony FX6 等有陀螺儀資料的機種），輸出符合專案畫面大小；需先到「設定 › 依賴元件」安裝 Gyroflow"
        : "從畫面內容分析晃動，任何影片都能用"}>
        <${Select} value=${draft.engine} options=${ENGINES} onChange=${(v) => set("engine", v)} />
      <//>
      ${gyro ? html`
      <${Field} label="平滑度" hint="越高越穩，但跟拍轉向會比較慢跟上；Gyroflow 預設 0.5">
        <${Slider} value=${draft.gyroSmooth} min=${0.05} max=${1} step=${0.05} format=${(v) => round(v, 2)}
          onChange=${(v) => set("gyroSmooth", v)} onCommit=${(v) => set("gyroSmooth", v)} />
      <//>
      <${GyroFolder} />` : html`
      <${Toggle} checked=${draft.tripod} onChange=${(v) => set("tripod", v)} label="腳架模式"
        hint="鎖定第一格的構圖，像架在腳架上；適合原本該是固定鏡頭的畫面" />
      ${MAIN.map(slider)}
      <${Field} label="邊緣處理">
        <${Select} value=${draft.optzoom} options=${OPTZOOM} onChange=${(v) => set("optzoom", v)} />
      <//>
      <${Field} label="露出的邊緣填">
        <${Select} value=${draft.crop} options=${CROP} onChange=${(v) => set("crop", v)} />
      <//>
      <details class="grade-sec">
        <summary>進階</summary>
        <div class="grade-sec-body">
          ${ADVANCED.map(num)}
          <${Field} label="鏡頭路徑演算法">
            <${Select} value=${draft.optalgo} options=${OPTALGO} onChange=${(v) => set("optalgo", v)} />
          <//>
          <${Field} label="內插">
            <${Select} value=${draft.interpol} options=${INTERPOL} onChange=${(v) => set("interpol", v)} />
          <//>
          <${Toggle} checked=${draft.relative} onChange=${(v) => set("relative", v)} label="相對前一格計算"
            hint="關掉改以絕對位置計算（腳架模式會自動關掉）" />
        </div>
      </details>`}
      <div class="row">
        <${Button} size="sm" kind="primary" disabled=${disabled || applied} onClick=${() => onApply(draft)}
          title="用這組設定從原始影片重新做一份防抖；做過同一組設定就直接切過去">${current ? "套用設定重新防抖" : "用這組設定防抖"}<//>
        <${Button} size="sm" kind="ghost" icon="undo" disabled=${same(normal(draft), normal({ ...STAB_DEFAULTS, engine: draft.engine }))}
          onClick=${() => setDraft({ ...STAB_DEFAULTS, engine: draft.engine })}>預設值<//>
      </div>
    </div>
  </details>`;
}
