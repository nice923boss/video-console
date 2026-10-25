// 雙語字幕 (timeline toolbar): a Chinese/English parallel script, such as a 中英對照 Word file, onto two caption rows,
// 中文 above English (vc/bilingual.py through POST /captions/bilingual). The dialog previews how the lines paired up
// and were cut; the import is one undo step, and speech alignment (vc/align.py) can then move the captions to where
// each line is said in the clips.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { api, upload } from "../lib/api.js";
import { clock } from "../lib/clock.js";
import { keyHint } from "../lib/keymap.js";
import { fmtSeconds, fmtTime, round } from "../lib/format.js";
import { decodeText } from "../lib/textfile.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, TextArea, Segmented, Toggle } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast, toastError } from "../components/toast.js";

const MAX_TEXT = 300_000; // vc/bilingual.py MAX_TEXT
const SHOWN = 60; // preview rows
const ROWS = ["中文", "English"]; // the caption tracks the import fills (vc/bilingual.py ZH_TRACK, EN_TRACK)
const ENGLISH = [
  { value: "double", label: "英文雙行", title: "英文太長時折成兩行，切段較少" },
  { value: "single", label: "英文單行", title: "英文只排一行，放不下就多切幾段" },
];

function Preview({ preview }) {
  if (!preview) return html`<p class="modal-text dim">貼上文字或開啟檔案後，這裡會顯示配對結果。</p>`;
  if (preview.error) return html`<div class="note note-warn"><${Icon} name="alert" size=${15} /><span class="note-text">${preview.error}</span></div>`;
  const { pairs, count, notes, segments } = preview;
  return html`
    <div class="note"><${Icon} name="info" size=${15} /><span class="note-text">
      ${pairs} 組中英對照，切成 ${count} 段，中文、English 軌各 ${count} 則字幕${notes.length ? `；${notes.join("；")}` : ""}
    </span></div>
    ${count > 0 && html`<div class="bi-list">
      ${segments.slice(0, SHOWN).map((s, i) => html`<div key=${i} class=${`bi-row${s.split ? " is-split" : ""}`}>
        <span class="mono dim bi-time">${fmtSeconds(s.start, 1)}</span>
        <span class="bi-text"><b>${s.zh}</b><span>${s.en}</span></span>
      </div>`)}
      ${count > SHOWN && html`<div class="bi-row dim">還有 ${count - SHOWN} 段</div>`}
    </div>`}`;
}

function BilingualBody({ project, close }) {
  const at = round(clock.time, 2);
  const hasClips = (project.shots || []).length > 0;
  const rowIds = new Set((project.captionTracks || []).filter((t) => ROWS.includes(t.name)).map((t) => t.id));
  const existing = (project.captions || []).filter((c) => rowIds.has(c.track)).length;
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [english, setEnglish] = useState("double");
  const [fromPlayhead, setFromPlayhead] = useState(false);
  const [replace, setReplace] = useState(true);
  const [align, setAlign] = useState(hasClips);
  const [preview, setPreview] = useState(null); // the dry run, or { error }
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);
  const path = `/api/projects/${encodeURIComponent(project.id)}/captions/bilingual`;
  const body = { text, english, start: fromPlayhead ? at : 0, replace: replace && existing > 0 };

  useEffect(() => {
    if (!text.trim()) return setPreview(null);
    let live = true;
    const t = setTimeout(() => api.post(path, { ...body, dryRun: true }).then((out) => live && setPreview(out),
      (err) => live && setPreview({ error: (err && err.message) || "無法預覽" })), 300);
    return () => { live = false; clearTimeout(t); };
  }, [text, english, body.start]);

  const openFile = async (e) => {
    const file = e.currentTarget.files[0];
    e.currentTarget.value = "";
    if (!file) return;
    try {
      const out = /\.docx$/i.test(file.name) ? await upload("/api/script/docx", file)
        : { text: decodeText(await file.arrayBuffer(), "auto").text, notes: [] };
      if (!out.text.trim()) return toast("檔案裡沒有文字", { level: "warn" });
      if (out.text.length > MAX_TEXT) return toast("檔案超過 30 萬字，無法匯入", { level: "warn" });
      setText(out.text);
      setName(file.name);
      if (out.notes && out.notes.length) toast(out.notes.join("；"));
    } catch (err) {
      toastError(err, "無法讀取檔案");
    }
  };
  const run = async () => {
    setBusy(true);
    try {
      const out = await api.post(path, { ...body, align: align && hasClips });
      close(true);
      toast(`已匯入 ${out.count} 段雙語字幕${out.job ? "，語音對齊已在背景開始（佇列可看進度）" : ""}`, { level: "success" });
      if (out.alignError) toast(`字幕已匯入，但無法開始語音對齊：${out.alignError}`, { level: "warn" });
    } catch (err) {
      setBusy(false);
      toastError(err, "無法匯入雙語字幕");
    }
  };

  return html`<${Modal} title="匯入雙語字幕" width=${760} onClose=${() => close(null)} backdropClose=${false}
      footer=${html`
        <input ref=${fileRef} type="file" accept=".docx,.txt,.md,text/plain" hidden onChange=${openFile} />
        <${Button} icon="folder-open" onClick=${() => fileRef.current.click()} title="Word（.docx）或文字檔">開啟檔案<//>
        <span class="grow dim bi-name">${name}</span>
        <${Button} onClick=${() => close(null)}>取消<//>
        <${Button} kind="primary" icon="captions" busy=${busy} disabled=${!preview || !preview.count} onClick=${run}>匯入<//>`}>
    <${Field} label="中英對照的文字" hint="一行中文、下一行是它的英文翻譯（翻譯分成兩段也可以）。空行與項目符號會略過，沒有翻譯的句子只上中文。">
      <${TextArea} value=${text} onChange=${(v) => { setText(v); if (!v) setName(""); }} rows=${7}
        placeholder="貼上文字，或按左下「開啟檔案」開啟 Word 檔" />
    <//>
    <div class="bi-options">
      <${Segmented} value=${english} onChange=${setEnglish} size="sm" options=${ENGLISH} />
      ${at > 0.1 && html`<${Toggle} checked=${fromPlayhead} onChange=${setFromPlayhead} label=${`從播放頭 ${fmtTime(at)} 開始`} />`}
    </div>
    ${existing > 0 && html`<${Toggle} checked=${replace} onChange=${setReplace} label="取代中文、English 軌上原有的字幕"
      hint=${replace ? `先刪除那兩條軌道上的 ${existing} 則字幕（含配音）` : "保留原有字幕，新字幕加在後面"} />`}
    <${Toggle} checked=${align && hasClips} onChange=${setAlign} disabled=${!hasClips} label="匯入後用語音對齊到影片"
      hint=${hasClips ? "在背景辨識影片裡說的話，把每句字幕移到說出來的位置，英文跟著中文移動（佇列可看進度）"
        : "專案還沒有鏡頭，字幕先依閱讀速度接續排列"} />
    <${Preview} preview=${preview} />
    <p class="modal-text dim">匯入是一個步驟，按復原${keyHint("app.undo")}可整批取消。</p>
  <//>`;
}

export function openBilingualImport(project) {
  return openModal((close) => html`<${BilingualBody} project=${project} close=${close} />`);
}
