// Settings page, notifications: desktop notices and the chime are handled by this page itself, ntfy
// push goes through the server so it also works while the window is closed.
import { html, useState, useEffect } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveSettings } from "../lib/actions.js";
import { chime, requestDesktopPermission } from "../lib/notify.js";
import { Button, IconButton, Field, Segmented, TextInput, Toggle } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast } from "../components/toast.js";
import { SecretField } from "../components/secret-field.js";

const TOPIC_RE = /^[A-Za-z0-9_-]{1,64}$/;
const URL_RE = /^https?:\/\/[^\s/]+/;
const ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789";
const PERMISSION_TEXT = {
  granted: { tone: "ok", text: "已允許系統通知" },
  denied: { tone: "danger", text: "系統通知被封鎖了。請到 Windows 設定 › 通知，或瀏覽器網址列左側的網站設定開啟" },
  default: { tone: "warn", text: "尚未允許。打開開關時會詢問一次" },
  unsupported: { tone: "warn", text: "這個瀏覽器不支援系統通知" },
};

const WHEN = [
  { value: "all", label: "每批都通知" },
  { value: "failed", label: "只在有失敗時" },
];

const currentPermission = () => ("Notification" in window ? Notification.permission : "unsupported");

function randomTopic() {
  const bytes = crypto.getRandomValues(new Uint8Array(18));
  return `vc-${[...bytes].map((b) => ALPHABET[b % ALPHABET.length]).join("")}`;
}

function ntfyError(d) {
  const topic = d.topic.trim();
  if (topic && !TOPIC_RE.test(topic)) return { topic: "只能用英數字、底線、連字號，最多 64 字" };
  if (d.enabled && !topic) return { topic: "開啟推播前，請先填寫主題" };
  if (!URL_RE.test(d.server.trim())) return { server: "必須是 http:// 或 https:// 開頭的網址" };
  return {};
}

function DeviceNotices({ notify }) {
  const [permission, setPermission] = useState(currentPermission);
  const perm = PERMISSION_TEXT[permission] || PERMISSION_TEXT.default;
  const setDesktop = async (desktop) => {
    if (desktop) setPermission(await requestDesktopPermission());
    saveSettings({ notify: { desktop } });
  };
  return html`
    <${Toggle} checked=${notify.desktop} onChange=${setDesktop} label="系統通知"
      hint="視窗不在前景時，工作完成或失敗會跳出 Windows 通知；點通知會回到對應的鏡頭" />
    ${notify.desktop && html`<p class=${`set-status tone-${perm.tone}`}>
      <${Icon} name=${perm.tone === "ok" ? "check-circle" : "alert"} size=${14} />${perm.text}</p>`}
    <div class="set-inline">
      <${Toggle} checked=${notify.sound} onChange=${(sound) => saveSettings({ notify: { sound } })} label="提示音"
        hint="完成時播放兩聲高音，失敗時播放兩聲低音" />
      <${Button} size="sm" kind="ghost" icon="bell" onClick=${() => { chime(true); setTimeout(() => chime(false), 700); }}>試聽<//>
    </div>`;
}

function NtfyForm({ saved }) {
  const savedKey = JSON.stringify(saved);
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState(null);
  useEffect(() => setDraft(saved), [savedKey]);

  const dirty = JSON.stringify(draft) !== savedKey;
  const errors = ntfyError(draft);
  const invalid = Object.keys(errors).length > 0;
  const set = (patch) => { setDraft((d) => ({ ...d, ...patch })); setResult(null); };
  const persist = async () => !dirty
    || !!(await saveSettings({ notify: { ntfy: { ...draft, topic: draft.topic.trim(), server: draft.server.trim() } } }));

  const save = async () => {
    setBusy("save");
    if (await persist()) toast("已儲存推播設定", { level: "success" });
    setBusy("");
  };
  const test = async () => {
    setBusy("test");
    setResult(null);
    if (await persist()) {
      try {
        setResult(await api.post("/api/notify/test"));
      } catch (err) {
        setResult({ error: true, message: err.message });
      }
    }
    setBusy("");
  };

  return html`<div class="set-ntfy">
    <${Toggle} checked=${draft.enabled} onChange=${(enabled) => set({ enabled })} label="手機推播（ntfy）"
      hint="在手機安裝 ntfy App 並訂閱同一個主題，工作完成或失敗時手機會收到通知" />
    <div class="row-2">
      <${Field} label="主題" error=${errors.topic}
          hint="主題名稱等同密碼，知道名稱的人都能訂閱。建議用隨機產生的長名稱">
        <div class="combo">
          <${TextInput} mono value=${draft.topic} maxLength=${64} spellcheck=${false} placeholder="vc-xxxxxxxx"
            onChange=${(topic) => set({ topic })} />
          <${IconButton} icon="refresh" size=${15} title="隨機產生主題" onClick=${() => set({ topic: randomTopic() })} />
        </div>
      <//>
      <${Field} label="伺服器" error=${errors.server} hint="使用公用服務填 https://ntfy.sh，也可以填自架的位址">
        <${TextInput} mono value=${draft.server} spellcheck=${false} onChange=${(server) => set({ server })} />
      <//>
    </div>
    <${Field} label="通知時機" hint="佇列全部跑完時送一則摘要；選「只在有失敗時」，全部成功就不推播，摘要只列失敗的工作">
      <${Segmented} size="sm" value=${draft.when || "all"} options=${WHEN} onChange=${(when) => set({ when })} />
    <//>
    <${SecretField} name="ntfy.token" label="存取權杖（選填）"
      help="自架或有權限控管的主題才需要，會以 Authorization: Bearer 標頭送到上面的伺服器。" />
    ${result && html`<div class=${`note ${result.error ? "note-danger" : "note-ok"}`} role="status">
      <${Icon} name=${result.error ? "x-circle" : "check-circle"} size=${15} />
      <span class="note-text">${result.error ? html`<b>傳送失敗</b>：${result.message}` : result.message}</span>
    </div>`}
    <footer class="eng-foot">
      ${dirty && html`<span class="eng-dirty">有未儲存的修改</span>`}
      <span class="grow"></span>
      ${dirty && html`<${Button} size="sm" kind="ghost" icon="undo" onClick=${() => { setDraft(saved); setResult(null); }}>還原<//>`}
      <${Button} size="sm" icon="zap" busy=${busy === "test"} disabled=${invalid || !draft.topic.trim() || !!busy} onClick=${test}>
        ${dirty ? "儲存並傳送測試" : "傳送測試"}<//>
      <${Button} size="sm" kind="primary" icon="save" busy=${busy === "save"} disabled=${!dirty || invalid || !!busy}
        onClick=${save}>儲存<//>
    </footer>
  </div>`;
}

export function NotifySection() {
  const notify = useStore((s) => s.settings.notify);
  return html`<div class="set-notify">
    <${DeviceNotices} notify=${notify} />
    <hr class="set-sep" />
    <${NtfyForm} saved=${notify.ntfy} />
  </div>`;
}
