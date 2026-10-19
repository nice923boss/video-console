// API key input. What the user types lives only in this component until it is sent to the server,
// which encrypts it with Windows DPAPI; afterwards the page only ever sees the masked form. The input
// is a text box drawn with masking dots rather than type=password, so the browser does not offer to
// save the key in its password manager.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { setSecret } from "../lib/actions.js";
import { Icon } from "./icons.js";
import { Field, Button, IconButton, Badge } from "./ui.js";
import { confirmDialog } from "./overlay.js";
import { toast } from "./toast.js";

const EMPTY = { set: false, masked: "" };

function StatusBadge({ status }) {
  if (status.unreadable) return html`<${Badge} tone="danger">無法解密<//>`;
  return status.set ? html`<${Badge} tone="ok">已設定<//>` : html`<${Badge} tone="neutral">未設定<//>`;
}

export function SecretField({ name, label, help = "", placeholder = "貼上金鑰", shared = "" }) {
  const status = useStore((s) => (s.secrets && s.secrets[name]) || EMPTY);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);
  const open = editing || !status.set || status.unreadable;

  // A different key slot (another provider) starts clean: never carry typed text across.
  useEffect(() => {
    setEditing(false);
    setValue("");
    setReveal(false);
  }, [name]);

  useEffect(() => {
    if (editing && inputRef.current) inputRef.current.focus();
  }, [editing]);

  const reset = () => {
    setValue("");
    setReveal(false);
    setEditing(false);
  };

  const save = async () => {
    const key = value.trim();
    if (!key) {
      toast("請先貼上金鑰", { level: "warn" });
      return;
    }
    setBusy(true);
    const ok = await setSecret(name, key);
    setBusy(false);
    if (ok) {
      reset();
      toast(`已儲存「${label}」`, { level: "success" });
    }
  };

  const clear = async () => {
    const yes = await confirmDialog({
      title: `清除${label}`,
      message: "清除後，使用這把金鑰的生成工作會無法送出，直到重新填寫。",
      confirmLabel: "清除",
      danger: true,
    });
    if (!yes) return;
    setBusy(true);
    const ok = await setSecret(name, "");
    setBusy(false);
    if (ok) {
      reset();
      toast(`已清除「${label}」`);
    }
  };

  const onKeyDown = (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      save();
    } else if (e.key === "Escape" && status.set && !status.unreadable) {
      e.preventDefault();
      e.stopPropagation();
      reset();
    }
  };

  const hint = [shared, help].filter(Boolean).join(" ");
  return html`<${Field} label=${label} hint=${hint} class="secret" after=${html`<${StatusBadge} status=${status} />`}>
    ${status.unreadable && html`<p class="secret-warn warn-text">
      <${Icon} name="alert" size=${14} />
      <span>已存的金鑰無法解密（可能換了 Windows 帳號或電腦），請重新填寫</span>
    </p>`}
    ${open
      ? html`<div class="secret-row">
          <div class="secret-input">
            <input ref=${inputRef} class=${`input mono ${reveal ? "" : "is-masked"}`} type="text" value=${value}
              placeholder=${placeholder} autocomplete="off" autocapitalize="off" spellcheck=${false}
              data-lpignore="true" data-1p-ignore="true" disabled=${busy} aria-label=${label}
              onInput=${(e) => setValue(e.currentTarget.value)} onKeyDown=${onKeyDown} />
            <${IconButton} icon=${reveal ? "eye-off" : "eye"} size=${15} title=${reveal ? "隱藏" : "顯示"}
              onClick=${() => setReveal(!reveal)} />
          </div>
          <${Button} size="sm" kind="primary" busy=${busy} disabled=${!value.trim()} onClick=${save}>儲存<//>
          ${status.set && !status.unreadable && html`<${Button} size="sm" kind="ghost" onClick=${reset}>取消<//>`}
          ${status.unreadable && html`<${Button} size="sm" kind="ghost" icon="trash" onClick=${clear}>清除<//>`}
        </div>`
      : html`<div class="secret-row">
          <span class="secret-mask mono" title="金鑰以 Windows 帳號加密保存，畫面上只顯示前後幾碼">
            <${Icon} name="lock" size=${13} />${status.masked}
          </span>
          <${Button} size="sm" kind="ghost" icon="pencil" onClick=${() => setEditing(true)}>更換<//>
          <${Button} size="sm" kind="ghost" icon="trash" disabled=${busy} onClick=${clear}>清除<//>
        </div>`}
  <//>`;
}
