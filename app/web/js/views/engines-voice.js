// Engines page, voice tab: the VoAI cloud key, then where Audio8Voice listens. Captions with a speaker
// are voiced one line per job: Audio8Voice on the ComfyUI lane, VoAI on its own cloud lane. Unlike the
// text model tab, the Audio8Voice test checks the typed address directly, so it works before saving.
// The VoAI test asks the server, which uses the saved key; the page never sees the key itself.
import { html, useState, useEffect } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveSettings } from "../lib/actions.js";
import { loadVoices } from "../lib/voice.js";
import { Button, Field, TextInput } from "../components/ui.js";
import { SecretField } from "../components/secret-field.js";
import { Icon } from "../components/icons.js";
import { toast } from "../components/toast.js";

const URL_RE = /^https?:\/\/[^\s/]+/;
const DEFAULT_URL = "http://127.0.0.1:18301";
const VOAI_KEYS_URL = "https://app.voai.ai/setting/api-keys";

function Result({ result }) {
  if (!result) return null;
  if (result.error) {
    return html`<div class="note note-danger" role="status">
      <${Icon} name="x-circle" size=${15} /><span class="note-text"><b>測試失敗</b>：${result.message}</span>
    </div>`;
  }
  const voice = result.voice_name || result.voice || "未選擇";
  return result.ready
    ? html`<div class="note note-ok" role="status"><${Icon} name="check-circle" size=${15} />
        <span class="note-text">Audio8Voice 已就緒，目前聲音：${voice}</span></div>`
    : html`<div class="note note-warn" role="status"><${Icon} name="alert" size=${15} />
        <span class="note-text">連得上 Audio8Voice，但模型還沒準備好（${result.status || "載入中"}），稍後再測一次。</span></div>`;
}

function VoaiResult({ result }) {
  if (!result) return null;
  if (!result.online) {
    return html`<div class="note note-danger" role="status">
      <${Icon} name="x-circle" size=${15} /><span class="note-text"><b>測試失敗</b>：${result.error || "還沒有設定金鑰"}</span>
    </div>`;
  }
  const counts = result.models.map((m) => `${m.version} ${m.speakers.length} 個`).join("、");
  return html`<div class="note note-ok" role="status"><${Icon} name="check-circle" size=${15} />
    <span class="note-text">VoAI 金鑰可用，聲音：${counts}</span></div>`;
}

function VoaiSection() {
  const keySet = useStore((s) => !!(s.secrets && s.secrets["voai.api_key"] && s.secrets["voai.api_key"].set));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  useEffect(() => setResult(null), [keySet]);

  const test = async () => {
    setBusy(true);
    setResult(null);
    try {
      setResult(await api.get("/api/voice/voai?refresh=1"));
    } catch (err) {
      setResult({ online: false, error: err.message });
    }
    setBusy(false);
    loadVoices(true);
  };

  return html`<section class="eng-voai">
    <h3 class="eng-voice-title">VoAI 雲端配音</h3>
    <p class="eng-intro">不用開本機服務，配音送到 VoAI 雲端合成，依字數扣 VoAI 點數，可以和 ComfyUI 同時進行。
      在人物表把人物的聲音來源切到「雲端」，再挑聲音、風格與模型。
      免費試用金鑰的成品可能帶 VoAI 的背景音樂與浮水印，正式使用請換付費金鑰。</p>
    <${SecretField} name="voai.api_key" label="VoAI API 金鑰"
      help="在 VoAI 網站的「設定 › API Keys」建立，金鑰以 Windows 帳號加密保存，不會出現在畫面或專案檔。" />
    <${VoaiResult} result=${result} />
    <footer class="eng-foot">
      <a class="eng-link" href=${VOAI_KEYS_URL} target="_blank" rel="noopener noreferrer">
        <${Icon} name="external" size=${13} />到 VoAI 管理金鑰</a>
      <span class="grow"></span>
      <${Button} size="sm" icon="plug" busy=${busy} disabled=${!keySet || busy} onClick=${test}
        title=${keySet ? "用已儲存的金鑰讀取 VoAI 聲音清單（不扣點數）" : "請先儲存金鑰"}>測試金鑰<//>
    </footer>
  </section>`;
}

export function VoiceTab() {
  const saved = useStore((s) => s.settings.voice);
  const savedUrl = (saved && saved.url) || DEFAULT_URL;
  const [url, setUrl] = useState(savedUrl);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState(null);
  useEffect(() => setUrl(savedUrl), [savedUrl]);

  const base = url.trim().replace(/\/+$/, "");
  const dirty = base !== savedUrl;
  const urlError = !base ? "請填寫服務位址" : URL_RE.test(base) ? "" : "必須是 http:// 或 https:// 開頭的網址";

  const save = async () => {
    setBusy("save");
    if (await saveSettings({ voice: { url: base } })) {
      toast("已儲存配音設定", { level: "success" });
      loadVoices();
    }
    setBusy("");
  };

  const test = async () => {
    setBusy("test");
    setResult(null);
    try {
      setResult(await api.post("/api/voice/test", { url: base }));
    } catch (err) {
      setResult({ error: true, message: err.message });
    }
    setBusy("");
  };

  return html`<div class="eng-pane">
    <p class="eng-intro">AI 配音可以用 VoAI 雲端或本機的 Audio8Voice 產生。在人物表替每個人物挑一個聲音，字幕指定人物後就能生成配音。</p>
    <${VoaiSection} />
    <h3 class="eng-voice-title eng-voice-split">Audio8Voice 本機配音</h3>
    <p class="eng-intro">本機 Audio8Voice 的配音工作和 ComfyUI 共用同一條佇列，一次只跑一個，不會同時搶顯示卡記憶體。
      角色預設（聲音加語速）在 Audio8Voice 視窗按「存成預設…」建立。</p>
    <${Field} label="服務位址" error=${urlError} hint=${`預設 ${DEFAULT_URL}，Audio8Voice 開著時才連得上`}>
      <${TextInput} mono value=${url} spellcheck=${false} placeholder=${DEFAULT_URL}
        onChange=${(v) => { setUrl(v); setResult(null); }} />
    <//>
    <${Result} result=${result} />
    <footer class="eng-foot">
      ${dirty && html`<span class="eng-dirty">有未儲存的修改</span>`}
      <span class="grow"></span>
      ${dirty && html`<${Button} size="sm" kind="ghost" icon="undo" onClick=${() => { setUrl(savedUrl); setResult(null); }}>還原<//>`}
      <${Button} size="sm" icon="plug" busy=${busy === "test"} disabled=${!!urlError || !!busy} onClick=${test}>測試連線<//>
      <${Button} size="sm" kind="primary" icon="save" busy=${busy === "save"} disabled=${!dirty || !!urlError || !!busy}
        onClick=${save}>儲存<//>
    </footer>
  </div>`;
}
