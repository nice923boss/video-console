// Import volume: asked when sound files come in, since lines made on the VoAI website come out quiet.
// The choice becomes the clip volume on the track (and the library item's starting volume for "加到音軌");
// the file itself is left as it is. The last choice is remembered and offered first next time. Files picked in the
// system's open-file dialog come with their folder (AU-09): a folder whose volume was remembered skips the question.
import { html, useState } from "../lib/h.js";
import { readPref } from "../lib/prefs.js";
import { openModal, Modal } from "../components/overlay.js";
import { toast } from "../components/toast.js";
import { Button, NumberInput, Toggle } from "../components/ui.js";

const KEY = "vc.import.gainDb";
const FOLDERS = "vc.import.folderGains"; // { folder (lower case, as Windows ignores case): dB }
const MIN_DB = -30;
const MAX_DB = 12; // clip volume tops out at 4, about +12 dB
export const CHOICES = [
  [6, "+6dB", "放大，適合音量偏小的 AI 配音（如 VoAI 網頁版）"],
  [0, "0dB", "維持原音量"],
  [-10, "-10dB", "降低，適合當背景音樂"],
];
const isDb = (v) => typeof v === "number" && v >= MIN_DB && v <= MAX_DB;

export const dbToGain = (db) => Math.round(10 ** (db / 20) * 10000) / 10000;
const dbLabel = (db) => `${db > 0 ? "+" : ""}${db}dB`;
const folderName = (folder) => folder.split(/[\\/]/).filter(Boolean).pop() || folder;
const folders = () => readPref(FOLDERS, {}, (v) => !!v && typeof v === "object" && !Array.isArray(v));

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Convenience only.
  }
}

function rememberFolder(folder, db) {
  const { [folder.toLowerCase()]: _, ...rest } = folders();
  save(FOLDERS, db == null ? rest : { ...rest, [folder.toLowerCase()]: db });
}

function GainBody({ count, folder, close }) {
  const last = readPref(KEY, 0, isDb);
  const [custom, setCustom] = useState(last);
  const [keep, setKeep] = useState(false);
  const pick = (db) => {
    save(KEY, db);
    if (folder && keep) rememberFolder(folder, db);
    close(db);
  };
  return html`<${Modal} title="匯入音量" width=${480} onClose=${() => close(null)}
      footer=${html`<span class="modal-foot-note dim">之後仍可在片段的音量欄位調整</span>
        <${Button} onClick=${() => close(null)}>取消<//>`}>
    <p class="modal-text">${count} 個聲音檔放到音軌時的音量。只改片段音量，不改原始檔。</p>
    ${folder && html`<${Toggle} class="gain-remember" checked=${keep} onChange=${setKeep}
      label=${`記住「${folderName(folder)}」資料夾的選擇`} hint="之後從這個資料夾匯入直接套用，不再詢問" />`}
    <div class="gain-choices">
      ${CHOICES.map(([db, label, hint]) => html`<${Button} key=${db} kind=${db === last ? "primary" : "default"}
          data-autofocus=${db === last || undefined} onClick=${() => pick(db)}>
        <b>${label}</b><span class="gain-hint">${hint}</span>
      <//>`)}
    </div>
    <div class="gain-custom">
      <span>其他</span>
      <${NumberInput} value=${custom} min=${MIN_DB} max=${MAX_DB} step=${1} digits=${1} suffix="dB" onCommit=${setCustom} />
      <${Button} size="sm" onClick=${() => pick(custom)}>套用<//>
    </div>
  <//>`;
}

// count: how many sound files are coming in; folder: where they came from, when known. Resolves to the chosen
// dB, or null when canceled.
export function pickImportGain(count, folder = null) {
  const saved = folder ? folders()[folder.toLowerCase()] : undefined;
  if (isDb(saved)) {
    toast(`已套用「${folderName(folder)}」資料夾記住的匯入音量 ${dbLabel(saved)}`, {
      action: { label: "改回每次詢問", fn: () => rememberFolder(folder, null) },
    });
    return Promise.resolve(saved);
  }
  return openModal((close) => html`<${GainBody} count=${count} folder=${folder} close=${close} />`);
}
