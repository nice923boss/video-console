// The AI assistant beside the preview (vc/agent.py): say what to change, optionally with a Word or text file attached
// (a bilingual script for the caption import). The text model reads the project and changes it through the same API;
// each change is one undo step named 「AI 助理：…」. Conversations stay while the page is open, one per project, and a
// turn keeps running when the panel is closed.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api, upload } from "../lib/api.js";
import { navigate } from "../lib/actions.js";
import { newId } from "../lib/format.js";
import { decodeText } from "../lib/textfile.js";
import { Button, IconButton, TextArea, Spinner } from "./ui.js";
import { Icon } from "./icons.js";
import { toast, toastError } from "./toast.js";

const MAX_TEXT = 300_000; // the server's limit for an attachment (vc/bilingual.py MAX_TEXT)
const HISTORY = 20; // the server keeps the last 20 messages
const TOOL_LABEL = {
  project_info: "讀取專案資訊",
  timeline: "讀取時間軸",
  list_captions: "讀取字幕",
  apply_ops: "修改專案",
  edit: "剪輯指令",
  import_bilingual: "匯入雙語字幕",
  align_captions: "語音對齊字幕",
  transcribe: "語音轉字幕",
  job_status: "查看工作進度",
  undo: "復原",
};
const EXAMPLES = [
  "把附件匯入成中英雙語字幕，英文單行，再用語音對齊到影片",
  "列出目前的字幕，找出錯字並改正",
  "在 10 秒的位置分割所有軌道",
];

// pid -> { messages, run }; run is the turn in flight: { id, steps } (steps arrive as "chat" events).
const chats = new Map();
const listeners = new Set();
const chatOf = (pid) => chats.get(pid) || { messages: [], run: null };

function update(pid, fn) {
  const cur = chatOf(pid);
  chats.set(pid, { ...cur, ...fn(cur) });
  for (const l of listeners) l();
}

window.addEventListener("vc-chat", (e) => {
  const { pid, id, step } = e.detail || {};
  const run = chatOf(pid).run;
  if (step && run && run.id === id) update(pid, (c) => ({ run: { ...c.run, steps: [...c.run.steps, step] } }));
});

async function send(pid, text, attachment) {
  const id = newId("ch");
  const user = { role: "user", content: text, attachment: attachment ? attachment.name : "" };
  const messages = [...chatOf(pid).messages, user].filter((m) => (m.role === "user" || m.role === "assistant") && !m.failed)
    .slice(-HISTORY).map((m) => ({ role: m.role, content: m.content }));
  update(pid, (c) => ({ messages: [...c.messages, user], run: { id, steps: [] } }));
  try {
    const out = await api.post(`/api/projects/${encodeURIComponent(pid)}/chat`, { id, messages, ...(attachment ? { attachment } : {}) });
    update(pid, (c) => ({ messages: [...c.messages, { role: "assistant", content: out.reply, steps: out.steps, stopped: out.stopped }], run: null }));
  } catch (err) {
    // The failed request stays on screen but is not sent again as history.
    update(pid, (c) => ({
      messages: [...c.messages.map((m) => (m === user ? { ...m, failed: true } : m)),
        // 409: the text model is off; 502: calling it failed (address, key, model), both fixed in its settings
        { role: "error", content: (err && err.message) || "對話失敗", llm: !!err && (err.status === 409 || err.status === 502) }],
      run: null,
    }));
  }
}

function stepText(s) {
  const a = s.args || {};
  const label = TOOL_LABEL[s.tool] || s.tool;
  const extra = s.tool === "apply_ops" ? a.label : s.tool === "edit" ? a.cmd : "";
  return extra ? `${label}：${extra}` : label;
}

function Steps({ steps }) {
  if (!steps || !steps.length) return null;
  return html`<ol class="chat-steps">
    ${steps.map((s) => html`<li class=${s.ok ? "" : "is-bad"} title=${s.error || ""}>
      <${Icon} name=${s.ok ? "check" : "x"} size=${12} />
      <span>${stepText(s)}${s.error && html`<span class="dim">（${s.error}）</span>`}</span>
    </li>`)}
  </ol>`;
}

function Message({ m }) {
  if (m.role === "error") {
    return html`<div class="chat-msg chat-error">
      <${Icon} name="alert-circle" size=${14} /><span>${m.content}</span>
      ${m.llm && html`<${Button} size="sm" onClick=${() => navigate("engines", { enginesTab: "llm" })}>前往設定<//>`}
    </div>`;
  }
  if (m.role === "user") {
    return html`<div class=${`chat-msg chat-user${m.failed ? " is-failed" : ""}`}>
      ${m.attachment && html`<span class="chat-file"><${Icon} name="file" size=${12} />${m.attachment}</span>`}
      <span class="chat-text">${m.content}</span>
    </div>`;
  }
  return html`<div class="chat-msg chat-bot">
    <${Steps} steps=${m.steps} />
    <span class="chat-text">${m.content}</span>
  </div>`;
}

export function ChatPanel({ project, onClose }) {
  const pid = project.id;
  const llmOn = useStore((s) => !!(s.settings && s.settings.llm && s.settings.llm.enabled));
  const [, setTick] = useState(0);
  const [draft, setDraft] = useState("");
  const [attachment, setAttachment] = useState(null);
  const [stopping, setStopping] = useState(false);
  const listRef = useRef(null);
  const fileRef = useRef(null);
  useEffect(() => {
    const l = () => setTick((n) => n + 1);
    listeners.add(l);
    return () => listeners.delete(l);
  }, []);
  const { messages, run } = chatOf(pid);
  useEffect(() => {
    const el = listRef.current;
    if (el && (messages.length || run)) el.scrollTop = el.scrollHeight; // the intro and its warning stay in view
  }, [messages.length, run && run.steps.length]);
  useEffect(() => setStopping(false), [run && run.id]);

  const submit = () => {
    const text = draft.trim();
    if (!text || run) return;
    setDraft("");
    send(pid, text, attachment);
  };
  const stop = async () => {
    setStopping(true);
    try {
      await api.post(`/api/projects/${encodeURIComponent(pid)}/chat/cancel`, { id: run.id });
    } catch (err) {
      setStopping(false);
      toastError(err, "無法停止");
    }
  };
  const openFile = async (e) => {
    const file = e.currentTarget.files[0];
    e.currentTarget.value = "";
    if (!file) return;
    try {
      const text = /\.docx$/i.test(file.name) ? (await upload("/api/script/docx", file)).text
        : decodeText(await file.arrayBuffer(), "auto").text;
      if (!text.trim()) return toast("檔案裡沒有文字", { level: "warn" });
      if (text.length > MAX_TEXT) return toast("檔案超過 30 萬字，無法附加", { level: "warn" });
      setAttachment({ name: file.name, text });
    } catch (err) {
      toastError(err, "無法讀取檔案");
    }
  };
  const onKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return html`<aside class="chat-panel" aria-label="AI 助理">
    <div class="chat-head">
      <${Icon} name="bot" size=${16} />
      <b class="grow">AI 助理</b>
      <${IconButton} icon="trash" size=${15} title="清除這段對話" disabled=${!!run || !messages.length}
        onClick=${() => update(pid, () => ({ messages: [] }))} />
      <${IconButton} icon="x" size=${15} title="關閉" onClick=${onClose} />
    </div>
    <div class="chat-list" ref=${listRef}>
      ${!messages.length && html`<div class="chat-intro">
        <p>用一般的話交代要改什麼，助理會讀取專案、剪輯與上字幕。每次修改都是一個「AI 助理：…」復原步驟，可以按復原取消。</p>
        ${!llmOn && html`<p class="chat-warn">尚未開啟文字模型，請先到「引擎 › 文字模型」開啟。
          <${Button} size="sm" onClick=${() => navigate("engines", { enginesTab: "llm" })}>前往設定<//></p>`}
        <p class="dim">例如：</p>
        ${EXAMPLES.map((t) => html`<button type="button" class="chat-example" onClick=${() => setDraft(t)}>${t}</button>`)}
      </div>`}
      ${messages.map((m) => html`<${Message} m=${m} />`)}
      ${run && html`<div class="chat-msg chat-bot is-running">
        <${Steps} steps=${run.steps} />
        <span class="chat-wait"><${Spinner} size=${12} />${stopping ? "停止中，等這一輪模型回覆完…" : "處理中…"}</span>
      </div>`}
    </div>
    <div class="chat-compose">
      ${attachment && html`<div class="chat-attach" title="每次送出都會附上，助理匯入雙語字幕時會用到">
        <${Icon} name="file" size=${12} /><span class="grow">${attachment.name}（${attachment.text.length.toLocaleString()} 字）</span>
        <${IconButton} icon="x" size=${13} title="移除附件" onClick=${() => setAttachment(null)} />
      </div>`}
      <${TextArea} value=${draft} onChange=${setDraft} rows=${3} placeholder="要改什麼？Enter 送出，Shift+Enter 換行"
        onKeyDown=${onKey} />
      <div class="chat-actions">
        <input type="file" ref=${fileRef} hidden accept=".docx,.txt,.md,.srt,text/plain" onChange=${openFile} />
        <${IconButton} icon="upload" size=${15} title="附加 Word 或文字檔（例如中英對照稿）"
          onClick=${() => fileRef.current && fileRef.current.click()} />
        <span class="grow"></span>
        ${run ? html`<${Button} size="sm" icon="stop" busy=${stopping} onClick=${stop}>停止<//>`
          : html`<${Button} size="sm" kind="primary" icon="arrow-right" disabled=${!draft.trim()} onClick=${submit}>送出<//>`}
      </div>
    </div>
  </aside>`;
}
