// Just enough Markdown for docs/使用說明.md on the 說明 page (views/help.js): headings, paragraphs, lists nested
// by indent, tables and code blocks, with inline code, bold and links. It builds vnodes, never HTML strings, so
// nothing in the text becomes markup.
import { html } from "./h.js";

const LIST_RE = /^(\s*)([-*]|(\d+)\.)\s+(.*)$/;
const TABLE_SEP_RE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const INLINE_RE = /`([^`]+)`|\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

const cells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
const plain = (text) => text.replace(INLINE_RE, (_, code, bold, label) => code || bold || label);

// GitHub's heading anchors, so links like README's 使用說明.md#11-下載工作流程到自己的-comfyui land on the
// same heading here.
export function slug(text) {
  return plain(text).trim().toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, "").replace(/\s/g, "-");
}

// Consecutive list lines as a tree: {type: "list", start (null when bulleted), items: [{text, lists}]}.
function listTree(entries, from, indent) {
  const first = entries[from];
  const list = { type: "list", start: first.start, items: [] };
  let i = from;
  while (i < entries.length && entries[i].indent >= indent) {
    const e = entries[i];
    if (e.indent > indent && list.items.length) {
      const sub = listTree(entries, i, e.indent);
      list.items[list.items.length - 1].lists.push(sub.list);
      i = sub.next;
    } else {
      list.items.push({ text: e.text, lists: [] });
      i += 1;
    }
  }
  return { list, next: i };
}

// Blocks: {type: "h", level, text, id} | {type: "p", text} | list (listTree) | {type: "table", head, rows}
// | {type: "code", text}.
export function parse(markdown) {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  const used = new Map();
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (!line.trim()) {
      i += 1;
    } else if (heading) {
      const base = slug(heading[2]);
      const n = used.get(base) || 0;
      used.set(base, n + 1);
      blocks.push({ type: "h", level: heading[1].length, text: heading[2].trim(), id: n ? `${base}-${n}` : base });
      i += 1;
    } else if (line.startsWith("```")) {
      const end = lines.findIndex((l, j) => j > i && l.startsWith("```"));
      const stop = end < 0 ? lines.length : end;
      blocks.push({ type: "code", text: lines.slice(i + 1, stop).join("\n") });
      i = stop + 1;
    } else if (line.trim().startsWith("|") && TABLE_SEP_RE.test(lines[i + 1] || "")) {
      const head = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      blocks.push({ type: "table", head, rows });
    } else if (LIST_RE.test(line)) {
      const entries = [];
      while (i < lines.length && lines[i].trim()) {
        const m = LIST_RE.exec(lines[i]);
        if (m) entries.push({ indent: m[1].length, start: m[3] ? Number(m[3]) : null, text: m[4] });
        else entries[entries.length - 1].text += ` ${lines[i].trim()}`; // a wrapped item
        i += 1;
      }
      for (let at = 0; at < entries.length;) {
        const { list, next } = listTree(entries, at, entries[at].indent);
        blocks.push(list);
        at = next;
      }
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*\|)/.test(lines[i]) && !LIST_RE.test(lines[i])) {
        para.push(lines[i++].trim());
      }
      blocks.push({ type: "p", text: para.join(" ") });
    }
  }
  return blocks;
}

// link(href, children) renders a link; the page decides what an in-page, web or file link does.
export function inline(text, link) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] != null) out.push(html`<code>${m[1]}</code>`);
    else if (m[2] != null) out.push(html`<b>${inline(m[2], link)}</b>`);
    else out.push(link(m[4], inline(m[3], link)));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function List({ list, link }) {
  const items = list.items.map((it, k) => html`<li key=${k}>${inline(it.text, link)}
    ${it.lists.map((sub, j) => html`<${List} key=${j} list=${sub} link=${link} />`)}</li>`);
  return list.start == null ? html`<ul>${items}</ul>` : html`<ol start=${list.start}>${items}</ol>`;
}

// One block; headings get the id `${idPrefix}${block.id}`.
export function Block({ block: b, link, idPrefix = "" }) {
  if (b.type === "h") return html`<${`h${b.level}`} id=${`${idPrefix}${b.id}`}>${inline(b.text, link)}<//>`;
  if (b.type === "p") return html`<p>${inline(b.text, link)}</p>`;
  if (b.type === "list") return html`<${List} list=${b} link=${link} />`;
  if (b.type === "code") return html`<pre><code>${b.text}</code></pre>`;
  return html`<div class="md-table"><table>
    <thead><tr>${b.head.map((c, k) => html`<th key=${k}>${inline(c, link)}</th>`)}</tr></thead>
    <tbody>${b.rows.map((r, j) => html`<tr key=${j}>${r.map((c, k) => html`<td key=${k}>${inline(c, link)}</td>`)}</tr>`)}</tbody>
  </table></div>`;
}
