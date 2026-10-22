// Engines page, speech-to-text tab: where Speech-to-subtitle lives. Its worker runs as a child process
// with its own Python; empty fields mean the sibling Speech-to-subtitle folder and the Python its
// launcher points at. The check runs the worker's self-test with the saved values, so it needs a save
// first, and it can take a minute or more (MFA starts slowly the first time).
import { html, useState, useEffect } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveSettings } from "../lib/actions.js";
import { Button, Field, TextInput } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast } from "../components/toast.js";

// [key in the check report, label, required to transcribe at all]
const ITEMS = [
  ["cuda", "顯示卡（CUDA）", true],
  ["model", "Whisper 模型", true],
  ["ffmpeg", "ffmpeg", false],
  ["mfa", "MFA 詞級對齊", false],
  ["opencc", "簡轉繁", false],
];
const TONE = { ok: ["note-ok", "check-circle"], warn: ["note-warn", "alert"], danger: ["note-danger", "x-circle"] };

function detail(key, r) {
  if (key === "cuda") return r.ok ? `${r.devices} 張可用` : r.message || "找不到可用的顯示卡";
  if (key === "model" || key === "ffmpeg") return r.ok ? r.path : r.message || "找不到";
  return r.message || (r.ok ? "可用" : "不可用");
}

function Note({ tone, children }) {
  const [cls, icon] = TONE[tone];
  return html`<div class=${`note ${cls}`} role="status"><${Icon} name=${icon} size=${15} />
    <span class="note-text">${children}</span></div>`;
}

function CheckResult({ result }) {
  if (!result) return null;
  if (result.error) return html`<${Note} tone="danger"><b>檢查失敗</b>：${result.message}<//>`;
  return html`
    <${Note} tone=${result.ok ? "ok" : "danger"}>
      ${result.ok ? "可以轉錄" : "無法轉錄：需要顯示卡與 Whisper 模型"}（${result.folder}）<//>
    ${ITEMS.map(([key, label, required]) => {
      const r = result[key] || {};
      return html`<${Note} key=${key} tone=${r.ok ? "ok" : required ? "danger" : "warn"}>
        <b>${label}</b>：<span class="mono">${detail(key, r)}</span><//>`;
    })}`;
}

export function SttTab() {
  const saved = useStore((s) => s.settings.stt) || {};
  const [folder, setFolder] = useState(saved.folder || "");
  const [python, setPython] = useState(saved.python || "");
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState(null);
  useEffect(() => { setFolder(saved.folder || ""); setPython(saved.python || ""); }, [saved.folder, saved.python]);

  const dirty = folder.trim() !== (saved.folder || "") || python.trim() !== (saved.python || "");

  const save = async () => {
    setBusy("save");
    if (await saveSettings({ stt: { folder: folder.trim(), python: python.trim() } })) {
      toast("已儲存語音轉字幕設定", { level: "success" });
      setResult(null);
    }
    setBusy("");
  };

  const check = async () => {
    setBusy("check");
    setResult(null);
    try {
      setResult(await api.post("/api/stt/check"));
    } catch (err) {
      setResult({ error: true, message: err.message });
    }
    setBusy("");
  };

  return html`<div class="eng-pane">
    <p class="eng-intro">鏡頭的影片有聲音時，在鏡頭屬性的「剪輯」分頁按「轉成字幕」，由本機的 Speech-to-subtitle 辨識修剪範圍內的原聲，
      字幕直接加到時間軸。轉錄和 ComfyUI、Audio8Voice 共用同一條佇列，一次只跑一個，不會同時搶顯示卡記憶體。</p>
    <p class="eng-intro">換到新電腦時，先執行 Speech-to-subtitle 資料夾裡的「安裝依賴.bat」，再回來按「檢查」。</p>
    <${Field} label="Speech-to-subtitle 資料夾" hint="留白：使用和 Video Console 同一層的 Speech-to-subtitle 資料夾">
      <${TextInput} mono value=${folder} spellcheck=${false} placeholder="F:\\AI Project\\Speech-to-subtitle"
        onChange=${(v) => { setFolder(v); setResult(null); }} />
    <//>
    <${Field} label="Python 執行檔" hint="留白：使用 Speech-to-subtitle 啟動器指向的 Python（pyvenv.cfg）">
      <${TextInput} mono value=${python} spellcheck=${false} placeholder="C:\\…\\Python312\\python.exe"
        onChange=${(v) => { setPython(v); setResult(null); }} />
    <//>
    ${busy === "check" && html`<${Note} tone="warn">檢查中，第一次啟動 MFA 可能要一分鐘左右<//>`}
    <${CheckResult} result=${result} />
    <footer class="eng-foot">
      ${dirty && html`<span class="eng-dirty">有未儲存的修改，儲存後才能檢查</span>`}
      <span class="grow"></span>
      ${dirty && html`<${Button} size="sm" kind="ghost" icon="undo"
        onClick=${() => { setFolder(saved.folder || ""); setPython(saved.python || ""); setResult(null); }}>還原<//>`}
      <${Button} size="sm" icon="plug" busy=${busy === "check"} disabled=${dirty || !!busy} onClick=${check}>檢查<//>
      <${Button} size="sm" kind="primary" icon="save" busy=${busy === "save"} disabled=${!dirty || !!busy}
        onClick=${save}>儲存<//>
    </footer>
  </div>`;
}
