// Pre-export check in the export dialog: what the exported video would show wrong (shots without a picture,
// missing files, things past the end), plus captions that look misspelt against the settings word list. Read
// once when the dialog opens; it only warns, the export still runs.
import { html, useState, useEffect } from "../lib/h.js";
import { api } from "../lib/api.js";
import { getState } from "../lib/store.js";
import { parseGlossary, glossaryHits } from "../lib/glossary.js";
import { Icon } from "../components/icons.js";

const SHOWN = 8;

function shots(numbers) {
  const list = numbers.slice(0, SHOWN).map((n) => `#${n}`).join("、");
  return numbers.length > SHOWN ? `${list} 等 ${numbers.length} 個` : list;
}

function files(names) {
  const list = names.slice(0, 3).join("、");
  return names.length > 3 ? `${list} 等 ${names.length} 個` : list;
}

export function exportWarnings(c) {
  const out = [];
  if (c.empty.length) out.push(`${c.empty.length} 個鏡頭還沒有畫面（${shots(c.empty)}），會以文字卡代替`);
  if (c.missing.length) out.push(`${c.missing.length} 個鏡頭的畫面檔案遺失（${shots(c.missing)}），會以文字卡代替`);
  if (c.disabled.length) out.push(`${c.disabled.length} 個鏡頭已停用（${shots(c.disabled)}），輸出時是黑畫面且沒有聲音`);
  if (c.missingAudio.length) out.push(`音軌檔案遺失：${files(c.missingAudio)}，輸出會少這些聲音`);
  if (c.missingOverlays.length) out.push(`子母畫面檔案遺失：${files(c.missingOverlays)}，輸出時略過`);
  if (c.audioPastEnd) out.push(`${c.audioPastEnd} 段音軌超過影片結尾，超出的部分會被裁掉`);
  if (c.captionsPastEnd) out.push(`${c.captionsPastEnd} 則字幕超過影片結尾，超出的部分不會出現`);
  if (c.videoHidden) out.push("鏡頭軌已隱藏，輸出的主畫面是黑色（子母畫面與字幕照常）");
  return out;
}

// Captions shown in the video whose text has a glossary hit (CP-12).
function typoWarning() {
  const { project, settings } = getState();
  const entries = parseGlossary(settings && settings.glossary);
  if (!entries.length || !project) return [];
  const n = (project.captions || []).filter((c) => (c.text || "").trim() && c.end > c.start && !c.disabled
    && glossaryHits(c.text, entries).length).length;
  return n ? [`${n} 則字幕可能有錯字（比對字幕用語詞庫），剪輯頁時間軸上有波浪底線標示`] : [];
}

export function ExportCheck({ pid }) {
  const [warnings, setWarnings] = useState(null);
  useEffect(() => {
    let alive = true;
    api.get(`/api/projects/${encodeURIComponent(pid)}/export-check`).then(
      (c) => alive && setWarnings([...exportWarnings(c), ...typoWarning()]),
      // The export itself reports the same problems, so a failed check only drops the list.
      () => alive && setWarnings(typoWarning()),
    );
    return () => { alive = false; };
  }, [pid]);
  if (!warnings || !warnings.length) return null;
  return html`<div class="note note-warn exp-check">
    <${Icon} name="alert" size=${15} />
    <div class="note-text">
      <b>輸出前檢查</b>
      <ul>${warnings.map((w) => html`<li>${w}</li>`)}</ul>
    </div>
  </div>`;
}
