// Project documents for the AI assistant (vc/projdocs.py): files kept in the project's docs folder, such as the
// script, the client's notes or reference pictures. The chat panel lists them, uploads new ones (button or drop)
// and lets a message mention one as @name; the server then gives the model its text or its picture.
import { html, useState, useEffect, useCallback } from "../lib/h.js";
import { api, upload } from "../lib/api.js";
import { fmtBytes } from "../lib/format.js";
import { IconButton } from "./ui.js";
import { Icon } from "./icons.js";
import { confirmDialog } from "./overlay.js";
import { toast, toastError } from "./toast.js";

// vc/projdocs.py TEXT_EXTS, DOC_EXTS and util.IMAGE_EXTS
export const DOC_ACCEPT = ".docx,.pdf,.txt,.md,.srt,.vtt,.csv,.tsv,.json,.ass,.png,.jpg,.jpeg,.webp,.bmp,.gif";
// The lyrics format align_lyrics reads (vc/lyrics.py parse_lyrics), to fill in and upload as a project document.
export const LYRICS_TEMPLATE = "/assets/lyrics-template.txt";
const MAX_FILE = 100 * 1024 ** 2; // vc/projdocs.py MAX_FILE
const MAX_MENTIONS = 8; // vc/agent.py MAX_DOCS
const docsPath = (pid) => `/api/projects/${encodeURIComponent(pid)}/docs`;

// The docs folder's listing; refresh() reads it again (files copied there by hand show up then).
export function useProjectDocs(pid) {
  const [docs, setDocs] = useState([]);
  const refresh = useCallback(async () => {
    try {
      setDocs((await api.get(docsPath(pid))).docs);
    } catch (err) {
      toastError(err, "無法讀取專案文件");
    }
  }, [pid]);
  useEffect(() => {
    refresh();
  }, [refresh]);
  return [docs, refresh];
}

// Uploads files to the docs folder one at a time; resolves to the names they were saved as (a name already taken
// gets " (2)" added).
export async function uploadDocs(pid, files) {
  const names = [];
  for (const file of files) {
    if (file.size > MAX_FILE) {
      toast(`「${file.name}」超過 100 MB，沒有上傳`, { level: "warn" });
      continue;
    }
    try {
      names.push((await upload(`${docsPath(pid)}?name=${encodeURIComponent(file.name)}`, file)).doc.name);
    } catch (err) {
      toastError(err, `無法上傳「${file.name}」`);
    }
  }
  return names;
}

// The @mention being typed just before the caret, { start, query }, or null. An @ right after a letter or digit
// (an e-mail address) starts none; the query runs to the caret on the same line. ＠ (full width) works too.
export function mentionAt(text, caret) {
  const head = text.slice(0, caret);
  const i = Math.max(head.lastIndexOf("@"), head.lastIndexOf("＠"));
  if (i < 0 || (i > 0 && /[A-Za-z0-9]/.test(head[i - 1]))) return null;
  const query = head.slice(i + 1);
  return query.length > 80 || /[\r\n]/.test(query) ? null : { start: i, query };
}

export function matchDocs(docs, query) {
  const q = query.trim().toLowerCase();
  return docs.filter((d) => d.name.toLowerCase().includes(q)).slice(0, 8);
}

// Names of the documents the text mentions as @name (or ＠name). Longest names first, so a name that is the start
// of a longer one is not counted for it.
export function mentionedDocs(text, docs) {
  let rest = text.replace(/＠/g, "@");
  const out = [];
  for (const d of [...docs].sort((a, b) => b.name.length - a.name.length)) {
    const tag = `@${d.name}`;
    if (!rest.includes(tag)) continue;
    out.push(d.name);
    rest = rest.split(tag).join(" ");
  }
  return out.slice(0, MAX_MENTIONS);
}

export function MentionMenu({ items, active, onPick }) {
  return html`<ul class="chat-mention" role="listbox" aria-label="專案文件">
    ${items.map((d, i) => html`<li role="option" aria-selected=${i === active} class=${i === active ? "is-active" : ""}
        onMouseDown=${(e) => {
          e.preventDefault(); // keeps the caret in the message box
          onPick(d);
        }}>
      <${Icon} name=${d.kind === "image" ? "image" : "file"} size=${12} /><span>${d.name}</span>
    </li>`)}
  </ul>`;
}

export function DocsDrawer({ pid, docs, refresh, onMention, onUpload }) {
  const remove = async (d) => {
    const ok = await confirmDialog({ title: `刪除「${d.name}」？`, message: "檔案會從專案的 docs 資料夾刪除，無法復原。",
      confirmLabel: "刪除", danger: true });
    if (!ok) return;
    try {
      await api.post(`${docsPath(pid)}/delete`, { name: d.name });
    } catch (err) {
      toastError(err, "無法刪除");
    }
    refresh();
  };
  const reveal = () => api.post(`${docsPath(pid)}/reveal`).catch((err) => toastError(err, "無法開啟資料夾"));
  return html`<div class="chat-docs">
    <div class="chat-docs-head">
      <b class="grow">專案文件</b>
      <${IconButton} icon="refresh" size=${14} title="重新讀取" onClick=${refresh} />
      <${IconButton} icon="folder" size=${14} title="開啟 docs 資料夾（直接放進去的檔案也算）" onClick=${reveal} />
      <${IconButton} icon="upload" size=${14} title="上傳文件" onClick=${onUpload} />
      <a class="icon-btn" href=${LYRICS_TEMPLATE} download="歌詞範例.txt" title="下載歌詞範例（KTV 歌詞字幕的格式）"
        aria-label="下載歌詞範例"><${Icon} name="download" size=${14} /></a>
    </div>
    ${docs.length ? html`<ul class="chat-docs-list">
      ${docs.map((d) => html`<li>
        <${Icon} name=${d.kind === "image" ? "image" : "file"} size=${12} />
        <button type="button" class="chat-doc-name" title=${`在訊息裡提到 @${d.name}`} onClick=${() => onMention(d.name)}>
          ${d.name}</button>
        <span class="dim">${fmtBytes(d.size)}</span>
        <${IconButton} icon="trash" size=${13} title="刪除" onClick=${() => remove(d)} />
      </li>`)}
    </ul>` : html`<p class="dim chat-docs-empty">把劇本、修改意見、參考圖拖到這個面板或按上傳。之後在訊息裡打 @ 選檔案，助理就會讀它。</p>`}
  </div>`;
}
