// Comments a client left on the review page (SB-13, lib/review.js): who wrote what and when, each one
// removable once it has been dealt with. Nothing is shown while there are none.
import { html } from "../lib/h.js";
import { fmtDateTime } from "../lib/format.js";
import { Section, IconButton, Badge } from "./ui.js";

export function ReviewSection({ items, onChange, id }) {
  if (!items || !items.length) return null;
  const drop = (i) => onChange(items.filter((_, k) => k !== i));
  return html`<${Section} title="客戶意見" icon="message" collapsible id=${id}
      actions=${html`<${Badge} tone="warn">${items.length}<//>`}>
    <ul class="review-list">${items.map((c, i) => html`<li key=${`${c.at}-${i}`} class="review-item">
      <div class="review-head">
        <b>${c.by || "客戶"}</b><span class="dim">${fmtDateTime(Date.parse(c.at))}</span>
        <${IconButton} icon="trash" size=${14} title="處理完了，刪除這則意見" onClick=${() => drop(i)} />
      </div>
      <p class="review-text">${c.text}</p>
    </li>`)}</ul>
  <//>`;
}
