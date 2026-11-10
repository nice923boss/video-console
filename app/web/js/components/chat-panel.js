// The AI assistant beside the preview (vc/agent.py): say what to change. The text model reads the project, looks at
// frames of the edit, reads the project documents a message mentions as @name (components/chat-docs.js) and changes
// the project through the same API; each change is one undo step named 「AI 助理：…」. A message can also carry up to
// two pictures of the preview. Conversations stay while the page is open, one per project, and a turn keeps running
// when the panel is closed.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { navigate } from "../lib/actions.js";
import { newId, fmtTime } from "../lib/format.js";
import { clock } from "../lib/clock.js";
import { grabFrame } from "../lib/scope-frame.js";
import { Button, IconButton, TextArea, Spinner } from "./ui.js";
import { Icon } from "./icons.js";
import { toast, toastError } from "./toast.js";
import { DOC_ACCEPT, LYRICS_TEMPLATE, useProjectDocs, uploadDocs, mentionAt, matchDocs, mentionedDocs, MentionMenu,
  DocsDrawer } from "./chat-docs.js";

const HISTORY = 20; // the server keeps the last 20 messages
const MAX_FRAMES = 2; // vc/agentlook.py MAX_FRAMES
const FRAME_W = 768; // vc/projdocs.py IMAGE_WIDTH
const TOOL_LABEL = {
  project_info: "讀取專案資訊",
  timeline: "讀取時間軸",
  list_captions: "讀取字幕",
  apply_ops: "修改專案",
  edit: "剪輯指令",
  import_bilingual: "匯入雙語字幕",
  align_captions: "語音對齊字幕",
  align_lyrics: "歌詞對齊（KTV）",
  transcribe: "語音轉字幕",
  job_status: "查看工作進度",
  undo: "復原",
  analyze_picture: "分析畫面",
  auto_color: "自動調色",
  auto_stabilize: "自動防抖",
  view_frame: "查看畫面",
  list_documents: "列出專案文件",
  read_document: "讀取文件",
  app_docs: "查閱說明文件",
  call_api: "呼叫 API",
};
const EXAMPLES = [
  "看一下 5 秒的畫面，亮度夠嗎",
  "照 @修改意見.docx 修改字幕",
  "列出目前的字幕，找出錯字並改正",
  "在 10 秒的位置分割所有軌道",
  "用 @歌詞範例.txt 上 KTV 歌詞字幕",
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

// docs: names of the project documents the text mentions; frames: [{name, url}] pictures of the preview.
async function send(pid, text, docs, frames) {
  const id = newId("ch");
  const user = { role: "user", content: text, docs, frames };
  const messages = [...chatOf(pid).messages, user].filter((m) => (m.role === "user" || m.role === "assistant") && !m.failed)
    .slice(-HISTORY).map((m) => ({ role: m.role, content: m.content }));
  update(pid, (c) => ({ messages: [...c.messages, user], run: { id, steps: [] } }));
  try {
    const out = await api.post(`/api/projects/${encodeURIComponent(pid)}/chat`,
      { id, messages, ...(docs.length ? { docs } : {}), ...(frames.length ? { frames } : {}) });
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
  const extra = s.tool === "apply_ops" ? a.label : s.tool === "edit" ? a.cmd : s.tool === "read_document" ? a.name
    : s.tool === "call_api" ? `${a.method || "GET"} ${a.path || ""}` : s.tool === "app_docs" ? a.section || a.find : "";
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

function Frames({ frames, onRemove }) {
  if (!frames || !frames.length) return null;
  return html`<div class="chat-frames">
    ${frames.map((f, i) => html`<figure title=${f.name}>
      <img src=${f.url} alt=${f.name} />
      ${onRemove && html`<${IconButton} icon="x" size=${12} title="移除這張畫面" onClick=${() => onRemove(i)} />`}
    </figure>`)}
  </div>`;
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
      <${Frames} frames=${m.frames} />
      ${m.docs && m.docs.map((n) => html`<span class="chat-file"><${Icon} name="file" size=${12} />${n}</span>`)}
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
  const [frames, setFrames] = useState([]);
  const [stopping, setStopping] = useState(false);
  const [docsOpen, setDocsOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [mention, setMention] = useState(null); // { start, query } while an @name is being typed
  const [active, setActive] = useState(0);
  const [docs, refreshDocs] = useProjectDocs(pid);
  const asideRef = useRef(null);
  const listRef = useRef(null);
  const composeRef = useRef(null);
  const fileRef = useRef(null);
  const grabRef = useRef(null);
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

  const box = () => composeRef.current && composeRef.current.querySelector("textarea");
  const items = mention ? matchDocs(docs, mention.query) : [];
  const putCaret = (pos) => requestAnimationFrame(() => {
    const el = box();
    if (!el) return;
    el.focus();
    el.setSelectionRange(pos, pos);
  });
  // Puts @name in the message: in place of the @query being typed, or at the end.
  const insertMention = (name) => {
    const tag = `@${name} `;
    const el = box();
    if (mention) {
      const end = el ? el.selectionStart : draft.length;
      setDraft(draft.slice(0, mention.start) + tag + draft.slice(end));
      putCaret(mention.start + tag.length);
    } else {
      const head = draft && !/\s$/.test(draft) ? `${draft} ` : draft;
      setDraft(head + tag);
      putCaret(head.length + tag.length);
    }
    setMention(null);
  };
  const onChange = (value) => {
    setDraft(value);
    const el = box();
    const next = el ? mentionAt(value, el.selectionStart) : null;
    if (next && !mention) refreshDocs(); // files put in the folder by hand show up in the menu
    setMention(next);
    setActive(0);
  };
  const addFiles = async (files) => {
    if (!files.length) return;
    const names = await uploadDocs(pid, files);
    refreshDocs();
    if (!names.length) return;
    toast(`已加入專案文件：${names.join("、")}`);
    const head = draft && !/\s$/.test(draft) ? `${draft} ` : draft;
    const tags = names.map((n) => `@${n} `).join("");
    setDraft(head + tags);
    putCaret(head.length + tags.length);
  };
  const attachFrame = () => {
    if (frames.length >= MAX_FRAMES) return toast(`一則訊息最多附上 ${MAX_FRAMES} 張畫面`, { level: "warn" });
    const graded = asideRef.current && asideRef.current.parentElement.querySelector(".pl-graded");
    const c = grabRef.current || (grabRef.current = { raw: document.createElement("canvas"),
      out: document.createElement("canvas"), jpg: document.createElement("canvas") });
    const data = graded ? grabFrame(graded, c.raw, c.out, FRAME_W) : null;
    if (!data) return toast("預覽畫面還沒準備好", { level: "warn" });
    c.jpg.width = data.width;
    c.jpg.height = data.height;
    c.jpg.getContext("2d").putImageData(data, 0, 0);
    setFrames([...frames, { name: `預覽畫面 ${fmtTime(clock.time)}`, url: c.jpg.toDataURL("image/jpeg", 0.85) }]);
  };

  const submit = () => {
    const text = draft.trim();
    if (!text || run) return;
    setDraft("");
    setFrames([]);
    setMention(null);
    send(pid, text, mentionedDocs(text, docs), frames);
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
  const onKey = (e) => {
    if (items.length && ["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key) && !e.isComposing) {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "ArrowDown") setActive((active + 1) % items.length);
      else if (e.key === "ArrowUp") setActive((active - 1 + items.length) % items.length);
      else if (e.key === "Escape") setMention(null);
      else insertMention(items[Math.min(active, items.length - 1)].name);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  };
  const hasFiles = (e) => !!e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  const onDragOver = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); // tells main.js's window handler this drop is taken
    e.dataTransfer.dropEffect = "copy";
    setDragging(true);
  };
  const onDragLeave = (e) => {
    if (!asideRef.current.contains(e.relatedTarget)) setDragging(false);
  };
  const onDrop = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    setDragging(false);
    addFiles([...e.dataTransfer.files]);
  };
  const openFiles = (e) => {
    const files = [...e.currentTarget.files];
    e.currentTarget.value = "";
    addFiles(files);
  };
  const pickFiles = () => fileRef.current && fileRef.current.click();

  return html`<aside class=${`chat-panel${dragging ? " is-dragging" : ""}`} aria-label="AI 助理" ref=${asideRef}
      onDragOver=${onDragOver} onDragLeave=${onDragLeave} onDrop=${onDrop}>
    <div class="chat-head">
      <${Icon} name="bot" size=${16} />
      <b class="grow">AI 助理</b>
      <${IconButton} icon="folder" size=${15} title="專案文件（劇本、修改意見、參考圖）" active=${docsOpen}
        badge=${docs.length || null} onClick=${() => setDocsOpen(!docsOpen)} />
      <${IconButton} icon="trash" size=${15} title="清除這段對話" disabled=${!!run || !messages.length}
        onClick=${() => update(pid, () => ({ messages: [] }))} />
      <${IconButton} icon="x" size=${15} title="關閉" onClick=${onClose} />
    </div>
    ${docsOpen && html`<${DocsDrawer} pid=${pid} docs=${docs} refresh=${refreshDocs} onMention=${insertMention}
      onUpload=${pickFiles} />`}
    <div class="chat-list" ref=${listRef}>
      ${!messages.length && html`<div class="chat-intro">
        <p>用一般的話交代要改什麼，助理會讀取專案、看畫面、剪輯與上字幕。每次修改都是一個「AI 助理：…」復原步驟，可以按復原取消。</p>
        <p>客戶的劇本、修改意見、參考圖：拖到這個面板或按上傳，存進專案文件，訊息裡打 ＠ 選檔案。相機按鈕會附上目前的預覽畫面。</p>
        <p>KTV 歌詞字幕：<a href=${LYRICS_TEMPLATE} download="歌詞範例.txt">下載歌詞範例</a>，照格式換成你的歌詞後拖進來，再說「用 ＠檔名 上 KTV 歌詞字幕」。</p>
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
    <div class="chat-compose" ref=${composeRef}>
      ${items.length > 0 && html`<${MentionMenu} items=${items} active=${Math.min(active, items.length - 1)}
        onPick=${(d) => insertMention(d.name)} />`}
      ${mention && !docs.length && html`<div class="chat-mention chat-mention-empty dim">專案還沒有文件：按上傳或把檔案拖進來</div>`}
      <${Frames} frames=${frames} onRemove=${(i) => setFrames(frames.filter((_, k) => k !== i))} />
      <${TextArea} value=${draft} onChange=${onChange} rows=${3} placeholder="要改什麼？打 ＠ 選專案文件。Enter 送出，Shift+Enter 換行"
        onKeyDown=${onKey} onBlur=${() => setMention(null)} />
      <div class="chat-actions">
        <input type="file" ref=${fileRef} hidden multiple accept=${DOC_ACCEPT} onChange=${openFiles} />
        <${IconButton} icon="upload" size=${15} title="上傳到專案文件（Word、PDF、文字檔、字幕、圖片）" onClick=${pickFiles} />
        <${IconButton} icon="camera" size=${15} title="附上目前的預覽畫面" disabled=${frames.length >= MAX_FRAMES}
          onClick=${attachFrame} />
        <span class="grow"></span>
        ${run ? html`<${Button} size="sm" icon="stop" busy=${stopping} onClick=${stop}>停止<//>`
          : html`<${Button} size="sm" kind="primary" icon="arrow-right" disabled=${!draft.trim()} onClick=${submit}>送出<//>`}
      </div>
    </div>
    ${dragging && html`<div class="chat-drop"><${Icon} name="upload" size=${22} /><span>放開加入專案文件</span></div>`}
  </aside>`;
}
