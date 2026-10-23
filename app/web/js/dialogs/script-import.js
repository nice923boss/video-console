// Script import: paste or open a script, split it into shots (rules or the text model), review and
// edit the result, then insert the shots into the open project.
import { html, useState, useRef } from "../lib/h.js";
import { getState, setState } from "../lib/store.js";
import { api, upload } from "../lib/api.js";
import { loadProject } from "../lib/actions.js";
import { fmtSeconds, shotLabel } from "../lib/format.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Field, TextArea, TextInput, NumberInput, Segmented, Select, Toggle, Badge } from "../components/ui.js";
import { Icon } from "../components/icons.js";
import { toast, toastError } from "../components/toast.js";

const MAX_TEXT = 100000;
const REPLACEMENT_CHAR = String.fromCharCode(0xfffd);
const BOM = String.fromCharCode(0xfeff);
const MAX_SHOTS = 200;
const MODES = [
  { value: "auto", label: "自動判斷", title: "表格、編號清單或段落，自動選擇拆法" },
  { value: "paragraphs", label: "依段落", title: "空行分隔的每一段是一個鏡頭" },
  { value: "lines", label: "依行", title: "每一行是一個鏡頭" },
  { value: "sentences", label: "依句子", title: "每個句號、問號、驚嘆號斷一個鏡頭" },
  { value: "single", label: "整段一個", title: "全部文字變成一個鏡頭" },
  { value: "llm", label: "文字模型", icon: "bot", title: "交給文字模型改寫成分鏡（需要在引擎頁設定）" },
];
const MODE_NAME = { auto: "自動判斷", paragraphs: "依段落", lines: "依行", sentences: "依句子", single: "整段一個",
  table: "表格", numbered: "編號清單", llm: "文字模型" };
const PACES = { slow: ["舒緩", "info"], medium: ["平穩", "neutral"], fast: ["緊湊", "warn"] };
const SAMPLE = `1. 清晨的海港，薄霧中漁船緩緩出港，海鷗盤旋。（遠景，緩慢推進）
字幕：天還沒亮，港口已經醒了。

2. 老船長站在船頭，皺紋裡藏著海風，望向遠方。（中景，固定鏡頭）
字幕：他在這片海上，走了四十年。

3. 太陽躍出海平面，金色光芒灑滿甲板。（特寫轉全景，向上搖鏡）
字幕：每一天，都是新的航程。`;

// Text files from Windows tools are often Big5; fall back when UTF-8 decoding hits invalid bytes.
async function readTextFile(file) {
  const buf = await file.arrayBuffer();
  const utf8 = new TextDecoder("utf-8").decode(buf);
  if (!utf8.includes(REPLACEMENT_CHAR)) return utf8.startsWith(BOM) ? utf8.slice(1) : utf8;
  try {
    return new TextDecoder("big5").decode(buf);
  } catch {
    return utf8;
  }
}

function ShotRow({ shot, index, checked, onCheck, onEdit, beatStart }) {
  const pace = PACES[shot.pace];
  return html`<div class=${`si-row ${checked ? "" : "is-off"} ${beatStart ? "is-beat-start" : ""}`}>
    <label class="si-check" title=${checked ? "取消勾選就不會匯入" : "勾選後匯入"}>
      <input type="checkbox" checked=${checked} onChange=${(e) => onCheck(e.currentTarget.checked)} />
      <span class="mono">${index + 1}</span>
    </label>
    <div class="si-fields">
      <div class="si-line">
        <${TextInput} value=${shot.name} placeholder="鏡頭名稱（可留白）" maxlength="60" class="si-name"
          onChange=${(name) => onEdit({ name })} />
        <${NumberInput} value=${shot.duration} nullable placeholder="預設" min=${0.5} max=${60} step=${0.1} digits=${2}
          suffix="秒" class="si-dur" title="鏡頭長度，留白用專案預設" onCommit=${(duration) => onEdit({ duration })} />
      </div>
      <${TextArea} value=${shot.prompt} rows=${2} autosize placeholder="畫面描述（生圖提示詞）" maxlength="4000"
        onChange=${(prompt) => onEdit({ prompt })} />
      ${(shot.motion || shot.camera || shot.beat) && html`<div class="si-meta">
        ${shot.beat && html`<${Badge} tone=${pace ? pace[1] : "neutral"} title="劇情段落與節奏，只在這一步參考，不會存進鏡頭">
          第 ${shot.beat} 段${pace ? `，${pace[0]}` : ""}<//>`}
        ${shot.camera && html`<${Badge} tone="blue"><${Icon} name="camera" size=${12} />${shot.camera}<//>`}
        ${shot.motion && html`<span class="dim" title="動態描述（生影片提示詞）"><${Icon} name="video" size=${12} />${shot.motion}</span>`}
      </div>`}
      <div class="si-caption">
        <${Icon} name="captions" size=${14} />
        <${TextInput} value=${shot.caption} placeholder="字幕（可留白）" maxlength="500"
          onChange=${(caption) => onEdit({ caption })} />
      </div>
    </div>
  </div>`;
}

function ScriptImportBody({ close }) {
  const s = getState();
  const project = s.project;
  const llmOn = !!(s.settings && s.settings.llm && s.settings.llm.enabled);
  const focusIndex = project ? project.shots.findIndex((x) => x.id === s.focus) : -1;
  const [text, setText] = useState("");
  const [mode, setMode] = useState("auto");
  const [detail, setDetail] = useState(true);
  const [result, setResult] = useState(null);
  const [checked, setChecked] = useState([]);
  const [where, setWhere] = useState("end");
  const [captions, setCaptions] = useState(true);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);

  const modes = MODES.map((m) => (m.value === "llm" && !llmOn
    ? { ...m, disabled: true, title: "要先到「引擎 › 文字模型」開啟並設定" } : m));
  const places = [
    { value: "end", label: `放在最後（第 ${project.shots.length + 1} 格起）` },
    { value: "start", label: "放在最前面" },
    ...(focusIndex >= 0 ? [{ value: "after", label: `放在 ${shotLabel(project.shots[focusIndex], focusIndex)} 之後` }] : []),
  ];

  const parse = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      const data = await api.post("/api/script/parse", { text, mode, detail });
      if (!data.shots.length) {
        toast("沒有拆出任何鏡頭，換一種拆分方式試試", { level: "warn" });
        return;
      }
      setResult(data);
      setChecked(data.shots.map((_, i) => i < MAX_SHOTS));
    } catch (err) {
      toastError(err, "拆解劇本失敗");
    } finally {
      setBusy(false);
    }
  };

  const edit = (i, patch) => setResult((r) => ({ ...r, shots: r.shots.map((x, k) => (k === i ? { ...x, ...patch } : x)) }));
  const picked = result ? result.shots.filter((_, i) => checked[i]) : [];
  const withCaption = picked.filter((x) => x.caption).length;
  const total = picked.reduce((n, x) => n + (x.duration || 0), 0);
  const lengths = picked.map((x) => x.duration).filter(Boolean);
  const beats = result ? new Set(result.shots.map((x) => x.beat).filter(Boolean)).size : 0;

  const apply = async () => {
    if (!picked.length || picked.length > MAX_SHOTS) return;
    const index = where === "start" ? 0 : where === "after" ? focusIndex + 1 : null;
    setBusy(true);
    try {
      const pid = project.id;
      const res = await api.post(`/api/projects/${encodeURIComponent(pid)}/script/apply`, { shots: picked, index, captions });
      await loadProject(pid);
      if (res.shotIds.length) setState({ selected: [res.shotIds[0]], focus: res.shotIds[0], view: "board" });
      toast(`已加入 ${res.added} 個鏡頭${res.captions ? `、${res.captions} 則字幕` : ""}`, { level: "success" });
      close(res);
    } catch (err) {
      toastError(err, "無法加入鏡頭");
      setBusy(false);
    }
  };

  const openFile = async (e) => {
    const file = e.currentTarget.files[0];
    e.currentTarget.value = "";
    if (!file) return;
    try {
      // Word files are read by the server: their shot tables come back as one tab-separated table.
      const docx = /\.docx$/i.test(file.name) ? await upload("/api/script/docx", file) : null;
      const content = docx ? docx.text : await readTextFile(file);
      setText(content.slice(0, MAX_TEXT));
      if (docx && docx.notes.length) toast(docx.notes.join("；"));
      if (content.length > MAX_TEXT) toast("檔案超過 10 萬字，只讀入前段", { level: "warn" });
    } catch (err) {
      toastError(err, "無法讀取檔案");
    }
  };

  if (result) {
    const allOn = checked.every(Boolean);
    const footer = html`
      <${Button} icon="arrow-left" onClick=${() => setResult(null)} disabled=${busy}>回到文字<//>
      <span class="grow"></span>
      <${Button} onClick=${() => close(null)}>取消<//>
      <${Button} kind="primary" icon="plus" busy=${busy} disabled=${!picked.length || picked.length > MAX_SHOTS}
        onClick=${apply}>加入 ${picked.length} 個鏡頭<//>`;
    return html`<${Modal} title="確認拆解結果" wide backdropClose=${false} onClose=${() => close(null)} footer=${footer} class="script-import">
      <div class="si-summary">
        <${Badge} tone="accent">${MODE_NAME[result.mode] || result.mode}<//>
        <span>${beats ? `${beats} 段劇情，` : ""}拆出 ${result.shots.length} 個鏡頭，已勾選 ${picked.length} 個${total > 0 ? `，有標長度的合計 ${fmtSeconds(total)}` : ""}${
          lengths.length > 1 ? `（每鏡 ${Math.min(...lengths)} 到 ${Math.max(...lengths)} 秒）` : ""}</span>
        <span class="grow"></span>
        <${Button} size="sm" kind="ghost" icon=${allOn ? "square" : "square-check"}
          onClick=${() => setChecked(result.shots.map(() => !allOn))}>${allOn ? "全部取消" : "全部勾選"}<//>
      </div>
      ${result.notes && result.notes.length > 0 && html`<div class="note">
        <${Icon} name="info" size=${15} /><span class="note-text">${result.notes.join("；")}</span>
      </div>`}
      ${picked.length > MAX_SHOTS && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
        <span class="note-text">一次最多加入 ${MAX_SHOTS} 個鏡頭，請取消勾選一些，或分次匯入。</span></div>`}
      <div class="si-list">
        ${result.shots.map((shot, i) => html`<${ShotRow} key=${i} shot=${shot} index=${i} checked=${!!checked[i]}
          beatStart=${i > 0 && !!shot.beat && shot.beat !== result.shots[i - 1].beat}
          onCheck=${(v) => setChecked((c) => c.map((x, k) => (k === i ? v : x)))} onEdit=${(patch) => edit(i, patch)} />`)}
      </div>
      <div class="row-2 si-options">
        <${Field} label="插入位置"><${Select} value=${where} options=${places} onChange=${setWhere} /><//>
        <${Toggle} checked=${captions} onChange=${setCaptions} label=${`建立字幕（${withCaption} 則）`}
          hint=${captions ? "字幕會對齊各鏡頭在序列中的時間" : "字幕文字改存到鏡頭備註"} />
      </div>
    <//>`;
  }

  const footer = html`
    <input ref=${fileRef} type="file" accept=".txt,.md,.csv,.tsv,.srt,.docx,text/plain" hidden onChange=${openFile} />
    <${Button} kind="ghost" icon="folder-open" title="文字檔（.txt、.md、.csv）或 Word 分鏡表（.docx）"
      onClick=${() => fileRef.current.click()}>開啟檔案<//>
    ${!text && html`<${Button} kind="ghost" icon="sparkles" onClick=${() => setText(SAMPLE)}>填入範例<//>`}
    <span class="grow"></span>
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="split" busy=${busy} disabled=${!text.trim()} onClick=${parse}>拆解成鏡頭<//>`;
  return html`<${Modal} title="從劇本建立鏡頭" wide backdropClose=${false} onClose=${() => close(null)} footer=${footer} class="script-import">
    <${Field} label="劇本或分鏡文字" after=${html`<span class="mono dim">${text.length.toLocaleString()} / 100,000</span>`}
      hint="支援編號清單、空行分段、Excel 貼上的表格（第一列是標題，例如「畫面、運鏡、字幕、秒數」），也可開啟 Word 分鏡表（.docx，例如「內容、影部、聲部」）">
      <${TextArea} value=${text} rows=${14} maxlength=${MAX_TEXT} data-autofocus onChange=${setText}
        placeholder=${"每段寫一個畫面，可加「字幕：」「運鏡：」「秒數：」等標籤。\n也可以直接貼上 Excel 或試算表的分鏡表。"}
        onKeyDown=${(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) parse(); }} />
    <//>
    <${Field} label="拆分方式" hint=${mode === "llm" ? `文字模型會花${detail ? "一到幾分鐘" : "幾秒到一分鐘"}，結果仍可在下一步修改` : "Ctrl+Enter 也可以開始拆解"}>
      <${Segmented} value=${mode} onChange=${setMode} options=${modes} size="sm" />
    <//>
    <${Toggle} checked=${detail} onChange=${setDetail} label="依劇情細拆鏡頭"
      hint=${detail ? "每段劇情拆成遠景、中景、特寫等多個鏡頭，依節奏與旁白長度安排秒數；已寫明秒數的段落維持一鏡"
        : "每段劇情一個鏡頭，照原文拆"} />
  <//>`;
}

// Needs an open project. Resolves with the apply result, or null when canceled.
export function openScriptImport() {
  if (!getState().project) {
    toast("請先開啟或建立一個專案", { level: "warn" });
    return Promise.resolve(null);
  }
  return openModal((close) => html`<${ScriptImportBody} close=${close} />`);
}
