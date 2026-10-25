// Add or edit one ComfyUI server: name, id (new servers only), address, enabled, and a connection test
// that uses the stored access token when the server already has one. A new server can take its token
// here too; it is sent to the encrypted store right after the server itself is saved. The server list is replaced as a
// whole on save, the same way the backend stores it.
import { html, useState } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { saveSettings, setSecret } from "../lib/actions.js";
import { fmtBytes } from "../lib/format.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, TextInput, Toggle } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { SecretField } from "../components/secret-field.js";
import { toast } from "../components/toast.js";

const ID_RE = /^[a-z0-9_-]{1,32}$/;
const URL_RE = /^https?:\/\/[^\s/]+/;

function suggestId(name, taken) {
  const base = name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24) || "server";
  let id = base;
  let n = 2;
  while (taken.has(id)) {
    id = `${base}-${n}`;
    n += 1;
  }
  return id;
}

function TestNote({ result }) {
  if (!result) return null;
  if (result.error) {
    return html`<div class="note note-danger" role="status">
      <${Icon} name="x-circle" size=${15} /><span class="note-text"><b>連線失敗</b>：${result.message}</span>
    </div>`;
  }
  const dev = result.device;
  const parts = [
    result.version && `ComfyUI ${result.version}`,
    dev && dev.name,
    dev && dev.vramTotal && `顯示卡記憶體 ${fmtBytes(dev.vramTotal)}`,
    result.latencyMs != null && `回應 ${result.latencyMs} ms`,
  ].filter(Boolean);
  return html`<div class="note note-ok" role="status">
    <${Icon} name="check-circle" size=${15} /><span class="note-text"><b>連線成功</b>${parts.length ? `：${parts.join("，")}` : ""}</span>
  </div>`;
}

function ServerDialog({ server, close }) {
  const servers = (getState().settings.comfy.servers || []);
  const isNew = !server;
  const taken = new Set(servers.map((s) => s.id));
  const [name, setName] = useState(server ? server.name : "");
  const [id, setId] = useState(server ? server.id : "");
  const [idTouched, setIdTouched] = useState(false);
  const [url, setUrl] = useState(server ? server.url : "http://127.0.0.1:8000");
  const [enabled, setEnabled] = useState(server ? server.enabled !== false : true);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState(null);
  const [token, setToken] = useState("");

  const effectiveId = isNew ? (idTouched ? id : suggestId(name || "server", taken)) : server.id;
  const urlClean = url.trim().replace(/\/+$/, "");
  const idError = isNew && (!ID_RE.test(effectiveId) ? "只能用小寫英數字、底線、連字號（最多 32 字）"
    : taken.has(effectiveId) ? "這個代號已經有伺服器使用" : "");
  const urlError = urlClean && !URL_RE.test(urlClean) ? "格式例如 http://127.0.0.1:8000" : "";
  const invalid = !name.trim() || !urlClean || !!idError || !!urlError;

  const test = async () => {
    setTesting(true);
    setResult(null);
    try {
      const res = await api.post("/api/comfy/test", { url: urlClean, token: isNew ? token.trim() : "", sid: isNew ? "" : server.id });
      setResult(res);
    } catch (err) {
      setResult({ error: true, message: err.message });
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    const entry = { id: effectiveId, name: name.trim(), url: urlClean, enabled };
    const next = isNew ? [...servers, entry] : servers.map((s) => (s.id === server.id ? entry : s));
    const saved = await saveSettings({ comfy: { servers: next } });
    if (!saved) {
      setSaving(false);
      return;
    }
    // On failure setSecret shows its own error; the server is saved, so the token can be set by editing it.
    if (isNew && token.trim()) await setSecret(`comfy.${entry.id}.token`, token.trim());
    setSaving(false);
    toast(isNew ? `已新增「${entry.name}」，正在檢查連線` : `已儲存「${entry.name}」`, { level: "success" });
    close(entry);
  };

  const footer = html`
    <${Button} icon="plug" busy=${testing} disabled=${!urlClean || !!urlError} onClick=${test}>測試連線<//>
    <span class="grow"></span>
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="save" busy=${saving} disabled=${invalid} onClick=${save}>${isNew ? "新增" : "儲存"}<//>`;

  return html`<${Modal} title=${isNew ? "新增 ComfyUI 伺服器" : `編輯「${server.name}」`} width=${540} backdropClose=${false}
      onClose=${() => close(null)} footer=${footer} class="comfy-server-dialog">
    <${Field} label="名稱" hint="顯示在佇列與鏡頭來源上，例如「工作室桌機」">
      <${TextInput} value=${name} maxLength=${60} placeholder="例如：工作室電腦" onChange=${setName} autofocus />
    <//>
    ${isNew && html`<${Field} label="代號" error=${idError} hint="程式內部用來辨識伺服器，新增後不能更改">
      <${TextInput} mono value=${effectiveId} maxLength=${32}
        onChange=${(v) => { setIdTouched(true); setId(v.trim().toLowerCase()); }} />
    <//>`}
    <${Field} label="位址" error=${urlError}
      hint="本機用 http://127.0.0.1:8000；另一台電腦可用區網 IP 或 Tailscale 網址。ComfyUI 需加上 --listen 才接受其他電腦連線">
      <${TextInput} mono value=${url} placeholder="http://127.0.0.1:8000" onChange=${(v) => { setUrl(v); setResult(null); }} />
    <//>
    <${Toggle} checked=${enabled} onChange=${setEnabled} label="啟用"
      hint="停用的伺服器不會被檢查，也不會接到生成工作" />
    <${TestNote} result=${result} />
    ${isNew
      ? html`<${Field} label="存取權杖（選填）"
          hint="ComfyUI 放在需要登入的反向代理後面時才需要，會以 Authorization: Bearer 標頭送出，並以 Windows 帳號加密保存。">
          <input class="input mono is-masked" type="text" value=${token} placeholder="貼上權杖" autocomplete="off"
            autocapitalize="off" spellcheck=${false} data-lpignore="true" data-1p-ignore="true" aria-label="存取權杖"
            onInput=${(e) => { setToken(e.currentTarget.value); setResult(null); }} />
        <//>`
      : html`<${SecretField} name=${`comfy.${server.id}.token`} label="存取權杖（選填）" placeholder="貼上權杖"
          help="ComfyUI 放在需要登入的反向代理後面時才需要，會以 Authorization: Bearer 標頭送出。" />`}
  <//>`;
}

// server: an entry from settings.comfy.servers, or null to add one. Resolves with the saved entry or null.
export function openServerDialog(server = null) {
  return openModal((close) => html`<${ServerDialog} server=${server} close=${close} />`);
}
