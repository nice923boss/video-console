// Subtitles too long to read in time (AI-04): every caption over the chosen characters per second is listed,
// the text model suggests shorter wording, and the ticked suggestions are applied as one undo step.
import { html, useState } from "../lib/h.js";
import { api } from "../lib/api.js";
import { getState } from "../lib/store.js";
import { ops, navigate } from "../lib/actions.js";
import { fmtSeconds } from "../lib/format.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Segmented, TextArea } from "../components/ui.js";
import { toast, toastError } from "../components/toast.js";

export const DEFAULT_RATE = 4.5;
const RATES = [4, 4.5, 5].map((r) => ({ value: r, label: `每秒 ${r} 字` }));
const MAX_BATCH = 200; // the server's limit per call (vc.llm.MAX_CONDENSE)
const GO_LLM = { label: "前往設定", fn: () => navigate("engines", { enginesTab: "llm" }) };

// Characters a viewer reads: spaces, punctuation and symbols do not count.
export function readChars(text) {
  return (text || "").replace(/[\s\p{P}\p{S}]/gu, "").length;
}

export function maxChars(cap, rate = DEFAULT_RATE) {
  return Math.max(1, Math.floor((cap.end - cap.start) * rate));
}

// Title cards (GR-10) are not read like subtitles, so they are never too long.
export function tooLong(cap, rate = DEFAULT_RATE) {
  return !cap.title && readChars(cap.text) > maxChars(cap, rate);
}

function CondenseDialog({ close, focusId }) {
  const [rate, setRate] = useState(DEFAULT_RATE);
  const [drafts, setDrafts] = useState({}); // id -> suggested text
  const [picked, setPicked] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const listAt = (r) => (getState().project.captions || [])
    .filter((c) => tooLong(c, r))
    .sort((a, b) => a.start - b.start);
  const caps = listAt(rate);
  const [ask, setAsk] = useState(() => new Set(caps.filter((c) => !focusId || c.id === focusId).map((c) => c.id)));
  const changeRate = (r) => {
    setRate(r);
    if (!focusId) setAsk(new Set(listAt(r).map((c) => c.id)));
  };
  const asking = caps.filter((c) => ask.has(c.id));
  const ready = caps.filter((c) => picked.has(c.id) && (drafts[c.id] || "").trim());

  const toggle = (set, setter, id, on) => {
    const next = new Set(set);
    if (on) next.add(id);
    else next.delete(id);
    setter(next);
  };

  const condense = async () => {
    const items = asking.slice(0, MAX_BATCH).map((c) => ({ id: c.id, text: c.text, max: maxChars(c, rate) }));
    setBusy(true);
    try {
      const res = await api.post("/api/llm/condense", { items });
      const got = Object.fromEntries((res.items || []).map((r) => [r.id, r.text]));
      setDrafts({ ...drafts, ...got });
      setPicked(new Set([...picked, ...Object.keys(got)]));
      const missed = items.length - Object.keys(got).length;
      if (missed > 0) toast(`有 ${missed} 句文字模型沒有回傳，可以再試一次`, { level: "warn" });
      if (asking.length > MAX_BATCH) toast(`一次最多濃縮 ${MAX_BATCH} 句，其餘請再按一次`, { level: "info" });
    } catch (err) {
      if (err && err.status === 409) toast(err.message, { level: "warn", title: "無法濃縮", action: GO_LLM });
      else toastError(err, "濃縮失敗");
    } finally {
      setBusy(false);
    }
  };

  const apply = () => {
    ops(ready.map((c) => ({ op: "caption.update", id: c.id, patch: { text: drafts[c.id].trim() } })),
      { label: `濃縮 ${ready.length} 句字幕` });
    toast(`已濃縮 ${ready.length} 句字幕，可用 Ctrl+Z 復原`, { level: "success" });
    close(true);
  };

  const footer = html`
    <${Button} onClick=${() => close(false)}>取消<//>
    <span class="grow"></span>
    <${Button} icon="wand" busy=${busy} disabled=${!asking.length}
      onClick=${condense}>用文字模型濃縮勾選的 ${asking.length} 句<//>
    <${Button} kind="primary" disabled=${busy || !ready.length} onClick=${apply}>套用 ${ready.length} 句<//>`;

  return html`<${Modal} title="濃縮過長的字幕" width=${720} backdropClose=${false} onClose=${() => close(false)}
      footer=${footer}>
    <div class="cc-head">
      <span class="dim">字數超過「顯示秒數 × 每秒字數」的字幕列在下面（空白與標點不算字）。</span>
      <span class="grow"></span>
      <${Segmented} size="sm" value=${rate} onChange=${changeRate} options=${RATES} />
    </div>
    ${caps.length ? html`<div class="cc-list">
      ${caps.map((c) => {
        const max = maxChars(c, rate);
        const draft = drafts[c.id];
        const n = draft == null ? 0 : readChars(draft);
        return html`<div key=${c.id} class="cc-row">
          <label class="cc-src">
            <input type="checkbox" checked=${ask.has(c.id)} onChange=${(e) => toggle(ask, setAsk, c.id, e.currentTarget.checked)} />
            <span class="mono dim">${fmtSeconds(c.start, 1)}</span>
            <span class="grow cc-text">${c.text}</span>
            <span class="dim cc-count">${readChars(c.text)} 字／${fmtSeconds(c.end - c.start, 1)}，上限 ${max} 字</span>
          </label>
          ${draft != null && html`<div class="cc-draft">
            <input type="checkbox" checked=${picked.has(c.id)} title="套用這句"
              onChange=${(e) => toggle(picked, setPicked, c.id, e.currentTarget.checked)} />
            <${TextArea} value=${draft} rows=${1} autosize onChange=${(v) => setDrafts({ ...drafts, [c.id]: v })} />
            <span class=${`cc-count ${n > max ? "is-over" : "dim"}`}>${n} 字</span>
          </div>`}
        </div>`;
      })}
    </div>` : html`<p class="modal-text">每秒 ${rate} 字的標準下，沒有過長的字幕。</p>`}
  <//>`;
}

// focusId: open with only that caption ticked (from the caption panel). Resolves true when applied.
export function openCaptionCondense(focusId = "") {
  if (!getState().project) return Promise.resolve(false);
  return openModal((close) => html`<${CondenseDialog} close=${close} focusId=${focusId} />`);
}
