// Dialog that picks a reference image for a shot: its own image takes, other shots' images, or
// images imported into the library. Resolves to the chosen take or media id, or null. `taken` lists the
// pictures already in use (the reference plus any extra references), highlighted in the grid.
import { html } from "../lib/h.js";
import { shotLabel } from "../lib/format.js";
import { Empty } from "./ui.js";
import { Thumb } from "./media.js";
import { openModal, Modal } from "./overlay.js";

const newest = (a, b) => (b.created || 0) - (a.created || 0);

function RefPicker({ project, shot, taken, close }) {
  const labels = new Map(project.shots.map((s, i) => [s.id, shotLabel(s, i)]));
  const images = Object.values(project.takes).filter((t) => t.kind === "image").sort(newest);
  const groups = [
    ["這個鏡頭的版本", images.filter((t) => t.shot === shot.id)],
    ["其他鏡頭的圖片", images.filter((t) => t.shot !== shot.id)],
    ["素材庫圖片", Object.values(project.media).filter((m) => m.kind === "image").sort(newest)],
  ].filter(([, list]) => list.length);
  const ratio = `${project.settings.width}/${project.settings.height}`;
  const nameOf = (x) => x.name || x.label || labels.get(x.shot) || "";

  return html`<${Modal} title="選擇參考圖" wide onClose=${() => close(null)}>
    ${groups.length ? groups.map(([title, list]) => html`<section key=${title} class="ref-group">
        <h4 class="ref-group-title">${title}<span class="dim">${list.length}</span></h4>
        <div class="ref-grid">
          ${list.map((x) => html`<button type="button" key=${x.id} title=${nameOf(x)}
              class=${`ref-cell ${taken.includes(x.id) ? "active" : ""}`} onClick=${() => close(x.id)}>
            <${Thumb} pid=${project.id} path=${x.thumb || x.file} ratio=${ratio} />
            <span class="ref-cell-label">${nameOf(x)}</span>
          </button>`)}
        </div>
      </section>`)
    : html`<${Empty} icon="images" title="還沒有可用的圖片">先生成圖片，或到「素材」頁匯入圖片後再回來選擇<//>`}
  <//>`;
}

export function openRefPicker(project, shot, taken = [shot.ref]) {
  return openModal((close) => html`<${RefPicker} project=${project} shot=${shot} taken=${taken} close=${close} />`);
}
