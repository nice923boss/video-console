// 說明 page: the user manual, docs/使用說明.md (GET /api/system/help), with its chapters on the left. The manual is
// the one copy of the help text; every change to what the app does updates it.
import { html, useState, useEffect, useMemo, useRef } from "../lib/h.js";
import { api } from "../lib/api.js";
import { parse, inline, Block } from "../lib/markdown.js";
import { Button, Empty, Spinner } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { openShortcuts } from "../dialogs/shortcuts.js";
import { openEditHelp } from "../dialogs/edit-help.js";
import { openTour } from "../dialogs/tour.js";

const ID = "help-"; // heading ids are prefixed so they cannot clash with the rest of the app
let cache = null; // the manual as last fetched; refetched each time the page opens

function jump(id) {
  const el = document.getElementById(`${ID}${id}`);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// In-page anchors scroll, web links open a browser tab, and the other files the manual names (README, docs/*.md)
// are only named: they sit in the install folder, outside what the app serves.
function link(href, children) {
  if (href.startsWith("#")) {
    return html`<a href=${href} onClick=${(e) => {
      e.preventDefault();
      jump(decodeURIComponent(href.slice(1)));
    }}>${children}</a>`;
  }
  if (/^https?:\/\//.test(href)) return html`<a href=${href} target="_blank" rel="noopener noreferrer">${children}</a>`;
  // Hrefs are relative to docs/, where the manual sits.
  const path = href.startsWith("../") ? href.slice(3) : `docs/${href}`;
  return html`<span class="help-file" title=${`Video Console 資料夾內的 ${path}`}>${children}</span>`;
}

export function HelpView() {
  const [got, setGot] = useState({ text: cache, error: "" });
  const [active, setActive] = useState("");
  const main = useRef(null);
  useEffect(() => {
    let live = true;
    api.get("/api/system/help")
      .then((res) => {
        cache = res.markdown;
        if (live) setGot({ text: res.markdown, error: "" });
      })
      .catch((err) => live && setGot((g) => ({ text: g.text, error: err.message || String(err) })));
    return () => {
      live = false;
    };
  }, []);
  const blocks = useMemo(() => (got.text ? parse(got.text) : []), [got.text]);
  const toc = blocks.filter((b) => b.type === "h" && (b.level === 2 || b.level === 3));

  const onScroll = () => {
    const box = main.current;
    if (!box || !toc.length) return;
    const top = box.getBoundingClientRect().top;
    let current = toc[0].id;
    for (const h of toc) {
      const el = document.getElementById(`${ID}${h.id}`);
      if (el && el.getBoundingClientRect().top - top <= 48) current = h.id;
    }
    if (box.scrollTop + box.clientHeight >= box.scrollHeight - 4) current = toc[toc.length - 1].id;
    setActive(current);
  };

  let body;
  if (!got.text && got.error) {
    body = html`<${Empty} icon="alert" title="無法讀取使用說明">${got.error}<//>`;
  } else if (!got.text) {
    body = html`<div class="help-loading dim"><${Spinner} size=${16} />正在讀取使用說明…</div>`;
  } else {
    body = html`<article class="help-doc">
      ${blocks.map((b, k) => html`<${Block} key=${k} block=${b} link=${link} idPrefix=${ID} />`)}
    </article>`;
  }
  return html`<div class="view view-help">
    <nav class="set-nav help-nav" aria-label="說明章節">
      <h2>說明</h2>
      <div class="help-tools">
        <${Button} size="sm" icon="keyboard" onClick=${() => openShortcuts()}>快捷鍵一覽<//>
        <${Button} size="sm" icon="book" onClick=${() => openEditHelp()}>剪輯功能說明<//>
        <${Button} size="sm" icon="book" onClick=${() => openTour()}>新手導覽<//>
      </div>
      ${toc.map((h) => html`<button type="button" key=${h.id}
          class=${`set-nav-item help-nav-item lv${h.level}${active === h.id ? " active" : ""}`}
          aria-current=${active === h.id ? "true" : undefined} onClick=${() => {
            jump(h.id);
            setActive(h.id);
          }}>
        ${h.level === 2 && html`<${Icon} name="book" size=${14} />`}<span>${inline(h.text, (_, c) => c)}</span>
      </button>`)}
    </nav>
    <div class="set-main help-main" ref=${main} onScroll=${onScroll}>${body}</div>
  </div>`;
}
