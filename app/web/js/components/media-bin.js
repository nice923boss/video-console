// The library's media beside the preview in the edit view (LB-02): drag a card onto a timeline row to place it
// there; Ctrl-click or Shift-click takes several at once, double-click previews.
import { html, useState, useMemo, useEffect } from "../lib/h.js";
import { fmtDuration } from "../lib/format.js";
import { usePref } from "../lib/prefs.js";
import { MEDIA_MIME, mediaMatches } from "../lib/media-actions.js";
import { Chip } from "./ui.js";
import { Icon } from "./icons.js";
import { Thumb, mediaEntry, openLightbox } from "./media.js";

const KINDS = [
  { id: "all", label: "全部" },
  { id: "image", icon: "image", title: "圖片" },
  { id: "video", icon: "film", title: "影片" },
  { id: "audio", icon: "music", title: "音訊" },
];
const KIND_ICON = { image: "image", video: "film", audio: "music" };

export function MediaBin({ project }) {
  const [kind, setKind] = usePref("vc.bin.kind", "all", (v) => KINDS.some((k) => k.id === v));
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(() => new Set());
  const [anchor, setAnchor] = useState(null);
  const pid = project.id;

  const list = useMemo(() => {
    const q = query.trim().toLowerCase();
    return Object.values(project.media || {}).filter((m) => !m.trashed && (kind === "all" || m.kind === kind) && mediaMatches(m, q))
      .sort((a, b) => (b.created || 0) - (a.created || 0));
  }, [project.media, kind, query]);
  useEffect(() => {
    if ([...sel].some((id) => !project.media[id])) setSel(new Set([...sel].filter((id) => project.media[id])));
  }, [project.media]);

  // Plain click picks one, Ctrl toggles one, Shift takes the run from the last plain or Ctrl click.
  const pick = (e, m) => {
    if (e.shiftKey && anchor) {
      const a = list.findIndex((x) => x.id === anchor);
      const b = list.findIndex((x) => x.id === m.id);
      if (a >= 0) return setSel(new Set(list.slice(Math.min(a, b), Math.max(a, b) + 1).map((x) => x.id)));
    }
    setAnchor(m.id);
    if (e.ctrlKey || e.metaKey) {
      const next = new Set(sel);
      if (next.has(m.id)) next.delete(m.id);
      else next.add(m.id);
      return setSel(next);
    }
    return setSel(new Set([m.id]));
  };
  const dragStart = (e, m) => {
    const ids = sel.has(m.id) ? list.filter((x) => sel.has(x.id)).map((x) => x.id) : [m.id];
    if (!sel.has(m.id)) setSel(new Set([m.id]));
    e.dataTransfer.setData(MEDIA_MIME, JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "copy";
  };
  const preview = (m) => {
    const visible = list.filter((x) => x.file);
    openLightbox(pid, visible.map((x) => mediaEntry(x)), Math.max(0, visible.indexOf(m)));
  };

  return html`<aside class="media-bin" aria-label="素材">
    <div class="bin-head">
      <div class="chips">
        ${KINDS.map((k) => html`<${Chip} key=${k.id} active=${kind === k.id} icon=${k.icon} title=${k.title}
          onClick=${() => setKind(k.id)}>${k.label}<//>`)}
      </div>
      <label class="search-box">
        <${Icon} name="search" size=${13} />
        <input type="search" value=${query} placeholder="搜尋素材" aria-label="搜尋素材"
          onInput=${(e) => setQuery(e.currentTarget.value)} />
      </label>
    </div>
    <div class="bin-list" role="listbox" aria-multiselectable="true">
      ${list.map((m) => html`<div key=${m.id} class=${`bin-item ${sel.has(m.id) ? "selected" : ""}`} role="option"
          aria-selected=${sel.has(m.id) ? "true" : "false"} draggable="true" title=${m.note || m.name}
          onClick=${(e) => pick(e, m)} onDblClick=${() => preview(m)} onDragStart=${(e) => dragStart(e, m)}>
        ${m.kind === "audio"
          ? html`<span class="bin-thumb bin-audio"><${Icon} name="music" size=${18} /></span>`
          : html`<${Thumb} pid=${pid} path=${m.thumb || (m.kind === "image" ? m.file : "")} ratio="16/10"
              icon=${KIND_ICON[m.kind] || "file"} class="bin-thumb" />`}
        <span class="bin-name">${m.name || "未命名"}</span>
        ${m.kind !== "image" && m.duration ? html`<span class="bin-dur dim mono">${fmtDuration(m.duration)}</span>` : null}
      </div>`)}
      ${!list.length && html`<p class="bin-empty dim">${Object.keys(project.media || {}).length ? "沒有符合的素材" : "素材庫是空的，先到素材庫匯入"}</p>`}
    </div>
    <div class="bin-foot dim">${sel.size > 1 ? `已選 ${sel.size} 個，` : ""}拖到時間軸的軌道上放置</div>
  </aside>`;
}
