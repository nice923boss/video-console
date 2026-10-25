// Asked once at start-up when saved settings still hold a default the app has since replaced
// (the Kling and Qwen picks before 2026-10-23). Each item can be switched or kept; closing with X or Esc
// leaves the question for the next start.
import { html, useState } from "../lib/h.js";
import { api } from "../lib/api.js";
import { setState } from "../lib/store.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Toggle } from "../components/ui.js";
import { toast, toastError } from "../components/toast.js";

function DefaultUpdatesDialog({ items, close }) {
  const [picked, setPicked] = useState(() => new Set(items.map((i) => i.key)));
  const [busy, setBusy] = useState(false);

  const answer = async (keys) => {
    setBusy(true);
    try {
      const res = await api.post("/api/settings/default-updates", { apply: keys });
      setState({ settings: res.settings, providers: res.providers });
      toast(keys.length ? `已切換 ${keys.length} 項到新的預設` : "已保留目前的設定", { level: keys.length ? "success" : "info" });
      close(true);
    } catch (err) {
      setBusy(false);
      toastError(err, "設定沒有更新");
    }
  };

  const toggle = (key, on) => {
    const next = new Set(picked);
    if (on) next.add(key);
    else next.delete(key);
    setPicked(next);
  };

  const footer = html`
    <${Button} disabled=${busy} onClick=${() => answer([])}>全部保留<//>
    <span class="grow"></span>
    <${Button} kind="primary" busy=${busy} disabled=${!picked.size} onClick=${() => answer([...picked])}>
      切換勾選的項目<//>`;

  return html`<${Modal} title="預設模型已更新" width=${560} backdropClose=${false} onClose=${() => close(false)}
      footer=${footer} class="default-updates">
    <p class="modal-text">雲端影片與文字模型的預設改成非中國廠商的模型。下列設定是之前儲存的舊預設，要切換嗎？
      沒切換的項目照舊使用，之後也能到「引擎」頁自行更改。</p>
    ${items.map((i) => html`<${Toggle} key=${i.key} checked=${picked.has(i.key)} onChange=${(on) => toggle(i.key, on)}
      label=${i.label} hint=${`${i.current} → ${i.suggested}`} />`)}
    ${items.some((i) => i.key === "llm") && html`<p class="dim">文字模型切換後，請先在 Ollama 執行 ollama pull ${
      items.find((i) => i.key === "llm").suggested} 下載模型。</p>`}
  <//>`;
}

// items: the server's defaultUpdates list. Resolves true once answered, false when closed without answering.
export function openDefaultUpdates(items) {
  return openModal((close) => html`<${DefaultUpdatesDialog} items=${items} close=${close} />`);
}
