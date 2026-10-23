// Settings page: app-wide preferences in sections with a jump list. Switches and pickers save at once;
// blocks with text or number fields keep a draft until "儲存".
import { html, useState, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { saveSettings } from "../lib/actions.js";
import { currentTheme } from "../lib/theme.js";
import { Button, Field, TextArea, NumberInput, Select, Segmented, Toggle, Section } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { AspectOptions } from "../dialogs/new-project.js";
import { KeymapEditor } from "./settings-keymap.js";
import { openHistory } from "../dialogs/history-dialog.js";
import { NotifySection } from "./settings-notify.js";
import { OutputSection, SystemSection } from "./settings-system.js";
import { DepsSection } from "./settings-deps.js";
import { useSectionDraft, DraftFoot } from "./settings-draft.js";

const NONE = [];
const FPS_CHOICES = [24, 25, 29.97, 30];
const THEMES = [
  { value: "dark", label: "深色", icon: "moon" },
  { value: "light", label: "淺色", icon: "sun" },
  { value: "system", label: "跟隨 Windows", icon: "panel" },
];

function Appearance() {
  const ui = useStore((s) => s.settings.ui);
  return html`<div class="set-appearance">
    <${Field} label="主題" hint=${ui.theme === "system" ? `目前套用${currentTheme() === "light" ? "淺色" : "深色"}，Windows 切換深淺色時跟著變` : ""}>
      <${Segmented} value=${ui.theme} options=${THEMES} onChange=${(theme) => saveSettings({ ui: { theme } })} />
    <//>
    <${Toggle} checked=${ui.confirmDelete !== false} onChange=${(confirmDelete) => saveSettings({ ui: { confirmDelete } })}
      label="刪除鏡頭前先確認" hint="關閉後直接刪除；刪錯了仍然可以按 Ctrl+Z 復原" />
  </div>`;
}

function sizeOptions(aspect, width, height) {
  const sizes = aspect ? aspect.sizes : [];
  const opts = sizes.map(([w, h], i) => ({ value: `${w}x${h}`, label: `${w} × ${h}${i === 0 ? "（預設）" : ""}` }));
  const current = `${width}x${height}`;
  return opts.some((o) => o.value === current) ? opts : [...opts, { value: current, label: `${width} × ${height}（自訂）` }];
}

function ProjectDefaults() {
  const saved = useStore((s) => s.settings.defaults);
  const routes = useStore((s) => s.settings.routes || NONE);
  const aspects = useStore((s) => (s.presets && s.presets.aspects) || NONE);
  const form = useSectionDraft(saved, (d) => ({ defaults: { ...d, negative: d.negative.trim() } }), "已儲存新專案預設");
  const { draft, set } = form;
  const aspect = aspects.find((a) => a.id === draft.aspect) || null;
  const fpsChoices = FPS_CHOICES.includes(draft.fps) ? FPS_CHOICES : [...FPS_CHOICES, draft.fps];
  const pickAspect = (a) => set({ aspect: a.id, width: a.sizes[0][0], height: a.sizes[0][1] });
  const pickSize = (v) => {
    const [width, height] = v.split("x").map(Number);
    set({ width, height });
  };
  return html`<div class="set-defaults">
    <p class="set-lead dim">新增專案時預先帶入這些值，建立後每個專案仍可以個別修改。</p>
    <${Field} label="畫面比例">
      <${AspectOptions} value=${draft.aspect} aspects=${aspects} onPick=${pickAspect} />
    <//>
    <div class="row-2">
      <${Field} label="輸出尺寸" hint="生圖與生影片都用這個尺寸">
        <${Select} value=${`${draft.width}x${draft.height}`} options=${sizeOptions(aspect, draft.width, draft.height)}
          onChange=${pickSize} />
      <//>
      <${Field} label="影格率">
        <${Segmented} value=${draft.fps} onChange=${(fps) => set({ fps })}
          options=${fpsChoices.map((v) => ({ value: v, label: `${v} fps` }))} />
      <//>
    </div>
    <div class="row-2">
      <${Field} label="新鏡頭長度" hint="新增鏡頭與劇本拆解時的預設秒數">
        <${NumberInput} value=${draft.shotDuration} min=${0.5} max=${60} step=${0.5} digits=${1} suffix="秒"
          onCommit=${(shotDuration) => set({ shotDuration })} />
      <//>
      <${Field} label="預設路由" hint="新鏡頭用哪條路由生圖、生影片">
        <${Select} value=${draft.route} onChange=${(route) => set({ route })}
          options=${routes.map((r) => ({ value: r.id, label: `${r.id}：${r.name}` }))} />
      <//>
    </div>
    <${Field} label="負向提示詞" hint="不希望畫面出現的內容，英文效果較好；專案風格的負向提示詞會再加在後面">
      <${TextArea} mono value=${draft.negative} rows=${3} autosize onChange=${(negative) => set({ negative })} />
    <//>
    <${DraftFoot} form=${form} />
  </div>`;
}

function HistorySettings() {
  const saved = useStore((s) => s.settings.history);
  const pid = useStore((s) => s.pid);
  const form = useSectionDraft(saved, (d) => ({ history: d }), "已儲存版本紀錄設定");
  const { draft, set } = form;
  return html`<div class="set-history">
    <p class="set-lead dim">修改專案時，距離上一份快照超過設定的間隔，伺服器會先存一份修改前的版本。
      快照和 Ctrl+Z 的復原紀錄分開保存，關閉程式後仍然留著。</p>
    <div class="row-2">
      <${Field} label="快照間隔" hint="10 到 3600 秒">
        <${NumberInput} value=${draft.intervalSec} min=${10} max=${3600} step=${30} digits=${0} suffix="秒"
          onCommit=${(intervalSec) => set({ intervalSec })} />
      <//>
      <${Field} label="每個專案保留" hint="5 到 500 份，超過時刪掉最舊的">
        <${NumberInput} value=${draft.keep} min=${5} max=${500} step=${5} digits=${0} suffix="份"
          onCommit=${(keep) => set({ keep })} />
      <//>
    </div>
    <${DraftFoot} form=${form}>
      ${pid && html`<${Button} size="sm" kind="ghost" icon="history" onClick=${openHistory}>查看目前專案的版本紀錄<//>`}
    <//>
  </div>`;
}

const SECTIONS = [
  { id: "deps", label: "依賴元件", icon: "package", view: DepsSection },
  { id: "appearance", label: "外觀與操作", icon: "palette", view: Appearance },
  { id: "defaults", label: "新專案預設", icon: "clapper", view: ProjectDefaults },
  { id: "output", label: "影片輸出", icon: "film", view: OutputSection },
  { id: "notify", label: "通知", icon: "bell", view: NotifySection },
  { id: "history", label: "版本紀錄", icon: "history", view: HistorySettings },
  { id: "shortcuts", label: "快捷鍵", icon: "keyboard", view: KeymapEditor },
  { id: "system", label: "資料與系統", icon: "drive", view: SystemSection },
];

export function SettingsView() {
  const main = useRef(null);
  const [active, setActive] = useState(SECTIONS[0].id);
  const jump = (id) => {
    const el = document.getElementById(`set-${id}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    setActive(id);
  };
  const onScroll = () => {
    const box = main.current;
    if (!box) return;
    if (box.scrollTop + box.clientHeight >= box.scrollHeight - 4) {
      setActive(SECTIONS[SECTIONS.length - 1].id);
      return;
    }
    const top = box.getBoundingClientRect().top;
    let current = SECTIONS[0].id;
    for (const s of SECTIONS) {
      const el = document.getElementById(`set-${s.id}`);
      if (el && el.getBoundingClientRect().top - top <= 48) current = s.id;
    }
    setActive(current);
  };
  return html`<div class="view view-settings">
    <nav class="set-nav" aria-label="設定分類">
      <h2>設定</h2>
      ${SECTIONS.map((s) => html`<button type="button" key=${s.id} class=${`set-nav-item${active === s.id ? " active" : ""}`}
          aria-current=${active === s.id ? "true" : undefined} onClick=${() => jump(s.id)}>
        <${Icon} name=${s.icon} size=${15} /><span>${s.label}</span>
      </button>`)}
    </nav>
    <div class="set-main" ref=${main} onScroll=${onScroll}>
      <p class="set-intro dim">開關與選單改了立即生效；有文字或數字欄位的區塊，按「儲存」後才生效。
        引擎、ComfyUI 與雲端 API 的設定在左側的「引擎」頁。</p>
      ${SECTIONS.map((s) => {
        const View = s.view;
        return html`<div class="set-block" id=${`set-${s.id}`} key=${s.id}>
          <${Section} title=${s.label} icon=${s.icon}><${View} /><//>
        </div>`;
      })}
    </div>
  </div>`;
}
