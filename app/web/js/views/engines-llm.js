// Engines page, text model tab: an OpenAI-compatible chat endpoint (Ollama, LM Studio, a cloud service)
// used to polish prompts and to turn a script into shots. The test reads the saved settings, so the
// form saves first and then tests.
import { html, useState, useEffect } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveSettings } from "../lib/actions.js";
import { Button, IconButton, Field, TextInput, Slider, Toggle, Chip } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { openMenu } from "../components/overlay.js";
import { toast } from "../components/toast.js";
import { SecretField } from "../components/secret-field.js";

const NONE = [];
const URL_RE = /^https?:\/\/[^\s/]+/;
const SERVICES = [
  { label: "Ollama（本機）", url: "http://127.0.0.1:11434/v1", hint: "不需要金鑰" },
  { label: "LM Studio（本機）", url: "http://127.0.0.1:1234/v1", hint: "不需要金鑰" },
  { label: "OpenAI", url: "https://api.openai.com/v1", hint: "需要 API 金鑰" },
  { label: "OpenRouter", url: "https://openrouter.ai/api/v1", hint: "需要 API 金鑰" },
];

function Result({ result, model, onPick }) {
  if (!result) return null;
  if (result.error) {
    return html`<div class="note note-danger" role="status">
      <${Icon} name="x-circle" size=${15} /><span class="note-text"><b>測試失敗</b>：${result.message}</span>
    </div>`;
  }
  const models = result.models || NONE;
  return html`<div class=${`note ${result.modelFound || !models.length ? "note-ok" : "note-warn"}`} role="status">
    <${Icon} name=${result.modelFound || !models.length ? "check-circle" : "alert"} size=${15} />
    <span class="note-text">${result.message}</span>
  </div>
  ${models.length > 0 && html`<${Field} label=${`服務提供的模型（${models.length}）`} hint="點一下套用，記得再按儲存">
    <div class="chips llm-models">
      ${models.map((m) => html`<${Chip} key=${m} active=${m === model} onClick=${() => onPick(m)}>${m}<//>`)}
    </div>
  <//>`}`;
}

export function LlmTab() {
  const saved = useStore((s) => s.settings.llm);
  const savedKey = JSON.stringify(saved);
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState(null);
  useEffect(() => setDraft(saved), [savedKey]);

  const dirty = JSON.stringify(draft) !== savedKey;
  const base = draft.baseUrl.trim().replace(/\/+$/, "");
  const urlError = !base ? "請填寫服務位址" : URL_RE.test(base) ? "" : "必須是 http:// 或 https:// 開頭的網址";
  const set = (patch) => setDraft((d) => ({ ...d, ...patch }));

  const persist = async () => {
    if (!dirty) return true;
    return !!(await saveSettings({ llm: { ...draft, baseUrl: base, model: draft.model.trim() } }));
  };

  const save = async () => {
    setBusy("save");
    if (await persist()) toast("已儲存文字模型設定", { level: "success" });
    setBusy("");
  };

  const test = async () => {
    setBusy("test");
    setResult(null);
    if (await persist()) {
      try {
        setResult(await api.post("/api/llm/test"));
      } catch (err) {
        setResult({ error: true, message: err.message });
      }
    }
    setBusy("");
  };

  const pickService = (e) => openMenu(e, [
    { header: "常用服務" },
    ...SERVICES.map((s) => ({ label: s.label, hint: s.hint, checked: s.url === base,
      onClick: () => { set({ baseUrl: s.url }); setResult(null); } })),
  ]);

  return html`<div class="eng-pane llm-pane">
    <p class="eng-intro">文字模型負責「潤飾提示詞」、「劇本 AI 拆解」，以及送 LTX-Video 前把中文提示詞翻成英文。任何相容 OpenAI Chat Completions 的服務都可以用，
      本機的 Ollama、LM Studio 不需要金鑰，資料也不會離開這台電腦。</p>
    <${Toggle} checked=${draft.enabled} onChange=${(enabled) => set({ enabled })} label="啟用文字模型"
      hint="關閉時，潤飾按鈕與 AI 拆解會提示先到這裡設定" />
    <${Field} label="服務位址" error=${urlError} hint="通常以 /v1 結尾">
      <div class="combo">
        <${TextInput} mono value=${draft.baseUrl} spellcheck=${false} placeholder="http://127.0.0.1:11434/v1"
          onChange=${(baseUrl) => { set({ baseUrl }); setResult(null); }} />
        <${IconButton} icon="chevron-down" size=${15} title="從常用服務挑選" onClick=${pickService} />
      </div>
    <//>
    <${Field} label="模型名稱" hint="Ollama 的寫法例如 gemma3:4b；按「儲存並測試」可以列出服務上的模型">
      <${TextInput} mono value=${draft.model} maxLength=${120} spellcheck=${false} onChange=${(model) => set({ model })} />
    <//>
    <${Field} label=${`溫度 ${Number(draft.temperature).toFixed(1)}`} hint="越低越保守穩定，越高越有變化；潤飾提示詞建議 0.5 到 0.8">
      <${Slider} value=${draft.temperature} min=${0} max=${2} step=${0.1} format=${(v) => Number(v).toFixed(1)}
        onChange=${(temperature) => set({ temperature })} />
    <//>
    <${SecretField} name="llm.api_key" label="API 金鑰（選填）"
      help="雲端服務才需要，會以 Authorization: Bearer 標頭送到上面的服務位址。" />
    <${Result} result=${result} model=${draft.model} onPick=${(model) => set({ model })} />
    <footer class="eng-foot">
      ${dirty && html`<span class="eng-dirty">有未儲存的修改</span>`}
      <span class="grow"></span>
      ${dirty && html`<${Button} size="sm" kind="ghost" icon="undo" onClick=${() => { setDraft(saved); setResult(null); }}>還原<//>`}
      <${Button} size="sm" icon="plug" busy=${busy === "test"} disabled=${!!urlError || !!busy} onClick=${test}>
        ${dirty ? "儲存並測試" : "測試連線"}<//>
      <${Button} size="sm" kind="primary" icon="save" busy=${busy === "save"} disabled=${!dirty || !!urlError || !!busy}
        onClick=${save}>儲存<//>
    </footer>
  </div>`;
}
