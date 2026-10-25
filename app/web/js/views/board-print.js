// Storyboard print sheet (SB-10): the shots shown on the board as a table a client can review on paper, with
// an empty column for their notes. It goes through the browser's print dialog, whose "Save as PDF" printer
// makes the PDF; while it prints, css/print.css hides the app and shows only this sheet.
import { html, render } from "../lib/h.js";
import { shotCover, shotCaptions } from "../lib/sequence.js";
import { fileUrl, fmtDate, fmtDuration, fmtSeconds, shotName } from "../lib/format.js";

function Sheet({ project, pid, rows, total }) {
  const ratio = `${project.settings.width}/${project.settings.height}`;
  return html`<header class="ps-head">
    <h1>${project.name}</h1>
    <p>${rows.length} 個鏡頭，序列長度 ${fmtDuration(total)}，${project.settings.width}×${project.settings.height}，列印日期 ${fmtDate(Date.now())}</p>
  </header>
  <table class="ps-table">
    <thead><tr><th class="ps-num">#</th><th class="ps-pic">畫面</th><th>內容</th><th class="ps-note">審稿意見</th></tr></thead>
    <tbody>${rows.map(({ shot, index, length }) => {
      const cover = shotCover(project, shot);
      const lines = shotCaptions(project, shot.id).map((c) => (c.text || "").trim()).filter(Boolean);
      return html`<tr key=${shot.id}>
        <td class="ps-num">${index + 1}</td>
        <td class="ps-pic"><div class="ps-frame" style=${`aspect-ratio:${ratio}`}>
          ${cover ? html`<img src=${fileUrl(pid, cover.path)} alt="" />` : html`<span>尚無畫面</span>`}
        </div></td>
        <td>
          <b>${shotName(shot, index)}</b><span class="ps-len">${fmtSeconds(length)}${shot.inSequence === false ? "，不在序列" : ""}</span>
          ${shot.prompt && html`<p>${shot.prompt}</p>`}
          ${(shot.camera || shot.motion) && html`<p class="ps-dim">運鏡：${[shot.camera, shot.motion].filter(Boolean).join("，")}</p>`}
          ${lines.length > 0 && html`<p class="ps-cap">字幕：${lines.join("／")}</p>`}
        </td>
        <td class="ps-note"></td>
      </tr>`;
    })}</tbody>
  </table>`;
}

// Prints the rows (board.js rows: {shot, index, length}) once their pictures have loaded.
export async function printBoard(project, pid, rows, total) {
  const sheet = document.createElement("div");
  sheet.className = "print-sheet";
  document.body.append(sheet);
  render(html`<${Sheet} project=${project} pid=${pid} rows=${rows} total=${total} />`, sheet);
  // A picture that fails to load prints as an empty frame rather than holding up the dialog.
  await Promise.all([...sheet.querySelectorAll("img")].map((img) => (img.complete ? null
    : new Promise((done) => { img.onload = done; img.onerror = done; }))));
  document.body.classList.add("is-printing");
  try {
    window.print();
  } finally {
    document.body.classList.remove("is-printing");
    render(null, sheet);
    sheet.remove();
  }
}
