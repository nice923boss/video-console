// Engines page: where images and videos come from. Routes pick a backend per shot, the other tabs set
// up ComfyUI servers, workflow templates, cloud APIs, the text model behind prompt polishing and the
// caption voice-over engines (the local Audio8Voice service and the VoAI cloud key), and the local
// Speech-to-subtitle that turns a clip's own sound into captions.
import { html } from "../lib/h.js";
import { useStore, setState } from "../lib/store.js";
import { Tabs } from "../components/ui.js";
import { comfyTone } from "../components/shell.js";
import { RoutesTab } from "./engines-routes.js";
import { ComfyTab } from "./engines-comfy.js";
import { WorkflowsTab } from "./engines-workflows.js";
import { ProvidersTab } from "./engines-providers.js";
import { LlmTab } from "./engines-llm.js";
import { VoiceTab } from "./engines-voice.js";
import { SttTab } from "./engines-stt.js";

const TABS = [
  { id: "routes", label: "路由", icon: "route", title: "每個鏡頭用哪個引擎生圖、生影片", view: RoutesTab },
  { id: "comfy", label: "ComfyUI", icon: "server", title: "本機或區網的 ComfyUI 伺服器", view: ComfyTab },
  { id: "workflows", label: "工作流程範本", icon: "workflow", title: "送給 ComfyUI 的流程與參數對應", view: WorkflowsTab },
  { id: "providers", label: "雲端 API", icon: "cloud", title: "雲端生圖與生影片服務的金鑰和參數", view: ProvidersTab },
  { id: "llm", label: "文字模型", icon: "bot", title: "潤飾提示詞、劇本拆解與 LTX 提示詞翻譯用的語言模型", view: LlmTab },
  { id: "voice", label: "配音", icon: "mic", title: "AI 配音用的 VoAI 雲端與 Audio8Voice 本機服務", view: VoiceTab },
  { id: "stt", label: "語音轉字幕", icon: "captions", title: "把影片原聲轉成字幕的本機 Speech-to-subtitle", view: SttTab },
];

const TONE_TEXT = { ok: "全部在線", warn: "部分離線", danger: "全部離線", neutral: "沒有啟用的伺服器" };

export function EnginesView() {
  const { tab, comfy, offline } = useStore((s) => ({
    tab: s.enginesTab, comfy: comfyTone(s.comfy),
    offline: (s.comfy || []).filter((c) => c.enabled !== false && !c.online).length,
  }));
  const current = TABS.find((t) => t.id === tab) || TABS[0];
  const tabs = TABS.map((t) => (t.id === "comfy"
    ? { ...t, badge: offline ? `${offline} 離線` : null, title: `${t.title}（${TONE_TEXT[comfy]}）` }
    : t));
  const View = current.view;
  return html`<div class="view view-engines">
    <header class="eng-head">
      <div class="eng-title">
        <h2>引擎</h2>
        <p class="dim">設定生圖、生影片、文字模型與 AI 配音的來源。修改會立即套用到之後送出的工作。</p>
      </div>
      <${Tabs} class="eng-tabs" value=${current.id} tabs=${tabs} onChange=${(enginesTab) => setState({ enginesTab })} />
    </header>
    <div class="eng-body" key=${current.id}>
      <${View} />
    </div>
  </div>`;
}
