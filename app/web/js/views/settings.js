// Settings page: app-wide preferences in sections with a jump list. Switches and pickers save at once;
// blocks with text or number fields keep a draft until "儲存".
import { html, useState, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { saveSettings } from "../lib/actions.js";
import { useTheme } from "../lib/theme.js";
import { Button, IconButton, Field, TextArea, DraftText, NumberInput, Select, Segmented, Toggle, Section } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast } from "../components/toast.js";
import { AspectOptions } from "../dialogs/new-project.js";
import { KeymapEditor } from "./settings-keymap.js";
import { undoKeyText } from "../lib/keymap.js";
import { openHistory } from "../dialogs/history-dialog.js";
import { NotifySection } from "./settings-notify.js";
import { OutputSection, SystemSection } from "./settings-system.js";
import { DepsSection } from "./settings-deps.js";
import { useSectionDraft, DraftFoot } from "./settings-draft.js";

const NONE = [];
const MAX_LABEL_COLORS = 12;
const FPS_CHOICES = [24, 25, 29.97, 30];
const THEMES = [
  { value: "dark", label: "深色", icon: "moon" },
  { value: "light", label: "淺色", icon: "sun" },
  { value: "system", label: "跟隨 Windows", icon: "panel" },
];
const TEXT_SCALES = [
  { value: 1, label: "標準" },
  { value: 1.15, label: "大" },
  { value: 1.3, label: "更大" },
];

function Appearance() {
  const ui = useStore((s) => s.settings.ui);
  const look = useTheme();
  return html`<div class="set-appearance">
    <${Field} label="主題" hint=${ui.theme === "system" ? `目前套用${look === "light" ? "淺色" : "深色"}，Windows 切換深淺色時跟著變` : ""}>
      <${Segmented} value=${ui.theme} options=${THEMES} onChange=${(theme) => saveSettings({ ui: { theme } })} />
    <//>
    <${Field} label="文字大小" hint="放大介面上的所有文字，不影響影片裡的字幕。高解析螢幕或看小字吃力時使用">
      <${Segmented} value=${ui.textScale || 1} options=${TEXT_SCALES} onChange=${(textScale) => saveSettings({ ui: { textScale } })} />
    <//>
    <${Toggle} checked=${ui.confirmDelete !== false} onChange=${(confirmDelete) => saveSettings({ ui: { confirmDelete } })}
      label="刪除鏡頭前先確認" hint=${`關閉後直接刪除；刪錯了仍然可以${undoKeyText()}復原`} />
    <${LabelColors} colors=${ui.labelColors || NONE} />
  </div>`;
}

// The label colours offered on shot cards and timeline clips (settings ui.labelColors), saved at once.
function LabelColors({ colors }) {
  const save = (labelColors) => saveSettings({ ui: { labelColors } });
  const pick = (i, value) => {
    if (value === colors[i].value) return;
    if (colors.some((c) => c.value === value)) return toast("這個顏色已經在清單裡", { duration: 2500 });
    save(colors.map((c, j) => (j === i ? { ...c, value } : c)));
  };
  const add = () => {
    let value;
    do value = `#${Math.floor(Math.random() * 0x1000000).toString(16).padStart(6, "0")}`;
    while (colors.some((c) => c.value === value));
    save([...colors, { value, label: "新顏色" }]);
  };
  return html`<${Field} label="標籤顏色" hint="分鏡卡片與剪輯頁片段可以標的顏色。改色碼不會改到已經標上舊顏色的鏡頭。">
    <div class="label-colors">
      ${colors.map((c, i) => html`<div class="label-color" key=${c.value}>
        <input type="color" value=${c.value} aria-label=${`${c.label}的色碼`} onChange=${(e) => pick(i, e.currentTarget.value)} />
        <${DraftText} value=${c.label} maxLength=${12} aria-label="顏色名稱"
          onCommit=${(label) => save(colors.map((x, j) => (j === i ? { ...x, label: label.trim() || x.value } : x)))} />
        <${IconButton} icon="trash" size=${15} title="移除這個顏色" onClick=${() => save(colors.filter((_, j) => j !== i))} />
      </div>`)}
      <${Button} size="sm" icon="plus" disabled=${colors.length >= MAX_LABEL_COLORS} onClick=${add}>新增顏色<//>
    </div>
  <//>`;
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
      快照和復原紀錄分開保存，關閉程式後仍然留著。</p>
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

function QueueSettings() {
  const saved = useStore((s) => s.settings.queue);
  const form = useSectionDraft(saved, (d) => ({ queue: d }), "已儲存工作佇列設定");
  const { draft, set } = form;
  return html`<div class="set-queue">
    <${Field} label="網路錯誤自動重試" hint=${"連線逾時、連不上、伺服器忙碌（429、502 到 504）時，等 15 秒後重新排隊，"
        + "每次間隔加倍。雲端任務已送出的，重試只接續查詢結果；送出當下就斷線的會重新送出，可能多扣一次點數。0 表示不重試"}>
      <${NumberInput} value=${draft.autoRetry} min=${0} max=${5} step=${1} digits=${0} suffix="次"
        onCommit=${(autoRetry) => set({ autoRetry })} />
    <//>
    <${DraftFoot} form=${form} />
  </div>`;
}

// The caption word list (CP-12): the edit page marks caption text that looks like a misspelt term.
function GlossarySettings() {
  const saved = { text: useStore((s) => s.settings.glossary) || "" };
  const form = useSectionDraft(saved, (d) => ({ glossary: d.text }), "已儲存字幕用語詞庫");
  const { draft, set } = form;
  return html`<div class="set-glossary">
    <p class="set-lead dim">每行一個正確的詞（產品名、人名）。剪輯頁會標出字幕裡跟它差一個字或只差大小寫的地方，
      三個字以上才比對。已知的錯字可接在「=」後面，用逗號分隔，例如「黃政文=黃正文,黃正雯」，短詞也能抓到。</p>
    <${TextArea} mono value=${draft.text} rows=${6} autosize onChange=${(text) => set({ text })} />
    <${DraftFoot} form=${form} />
  </div>`;
}

const SECTIONS = [
  { id: "deps", label: "依賴元件", icon: "package", view: DepsSection },
  { id: "appearance", label: "外觀與操作", icon: "palette", view: Appearance },
  { id: "defaults", label: "新專案預設", icon: "clapper", view: ProjectDefaults },
  { id: "glossary", label: "字幕用語詞庫", icon: "captions", view: GlossarySettings },
  { id: "output", label: "影片輸出", icon: "film", view: OutputSection },
  { id: "notify", label: "通知", icon: "bell", view: NotifySection },
  { id: "queue", label: "工作佇列", icon: "queue", view: QueueSettings },
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
