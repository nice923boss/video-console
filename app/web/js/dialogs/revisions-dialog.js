// Client revision notes (vc/revisions.py): load what the client packed on the review page (a ZIP, or the folder
// it unpacks to), go through each note on its frame, mark it 同意修改 / 微調 / 不採用, then apply the accepted ones.
// A picture or clip goes in on a blank shot at its frame, a soundtrack replaces the background music; a written
// note goes to the AI assistant. Right after loading, the notes can move to a copy so the original stays as it was.
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { getState } from "../lib/store.js";
import { api, request, upload } from "../lib/api.js";
import { openProject } from "../lib/actions.js";
import { fmtTime, fmtDateTime } from "../lib/format.js";
import { openModal, Modal, confirmDialog } from "../components/overlay.js";
import { Button, Badge, DraftText, Empty, Progress, Segmented, Select, Spinner } from "../components/ui.js";
import { toast, toastError } from "../components/toast.js";

const enc = encodeURIComponent;
const STATUS = { done: ["ok", "已套用"], manual: ["warn", "需人工處理"], failed: ["danger", "套用失敗"] };
const DECISIONS = [
  { value: "agree", label: "同意修改", title: "照客戶原話修改" },
  { value: "tweak", label: "微調", title: "照客戶意見，再加上我方補充的做法修改" },
  { value: "skip", label: "不採用" },
];
const DECIDED = { agree: "同意", tweak: "微調", skip: "不採用" };
const RED = "#ff3b30";

const accepted = (item) => (item.decision === "agree" || item.decision === "tweak") && item.status !== "done";

function kindLabel(item) {
  if (item.kind === "general") return "整體意見";
  if (item.kind === "music") return "更換配樂";
  if (item.kind === "insert") return item.insert && item.insert.type === "video" ? "插入影片" : "插入圖片";
  return "文字意見";
}

function where(item) {
  if (item.kind === "general") return "整支影片";
  if (item.kind === "music") return "整支影片的配樂";
  return `${fmtTime(item.t)}，鏡頭 #${item.no}${item.shotName ? `「${item.shotName}」` : ""}`;
}

// The client's marks, drawn the way the review page drew them (review_kit/page.js) on a w x h frame.
function Marks({ marks, w, h }) {
  const sw = Math.max(2, h * 0.006);
  const size = h * 0.045;
  return marks.map((m, i) => {
    if (m.type === "rect") {
      return html`<rect key=${i} x=${m.x * w} y=${m.y * h} width=${m.w * w} height=${m.h * h} rx=${sw} fill="none"
        stroke=${RED} stroke-width=${sw} />`;
    }
    if (m.type === "arrow") {
      const x1 = m.x1 * w, y1 = m.y1 * h, x2 = m.x2 * w, y2 = m.y2 * h;
      const a = Math.atan2(y2 - y1, x2 - x1);
      const len = Math.max(sw * 4, h * 0.03);
      const head = [[x2, y2], [x2 - len * Math.cos(a - 0.45), y2 - len * Math.sin(a - 0.45)],
        [x2 - len * Math.cos(a + 0.45), y2 - len * Math.sin(a + 0.45)]].map((p) => p.join(",")).join(" ");
      return html`<g key=${i}>
        <line x1=${x1} y1=${y1} x2=${x2 - len * 0.6 * Math.cos(a)} y2=${y2 - len * 0.6 * Math.sin(a)} stroke=${RED}
          stroke-width=${sw} stroke-linecap="round" />
        <polygon points=${head} fill=${RED} />
      </g>`;
    }
    return html`<text key=${i} x=${m.x * w} y=${m.y * h} font-size=${size} fill=${RED} stroke="#fff"
      stroke-width=${size * 0.18} stroke-linejoin="round" paint-order="stroke" font-weight="700">${m.text}</text>`;
  });
}

// The review video held on the note's frame, with the client's marks over it while it stays there.
function Frame({ src, t, marks }) {
  const ref = useRef(null);
  const [dims, setDims] = useState([1920, 1080]);
  const [onFrame, setOnFrame] = useState(true);
  const [error, setError] = useState(false);
  const seek = () => {
    const v = ref.current;
    if (v && v.readyState >= 1) v.currentTime = Math.min(t, Math.max(0, v.duration - 0.05));
  };
  useEffect(seek, [t]);
  const [w, h] = dims;
  if (error) {
    return html`<div class="note note-warn"><span class="note-text">找不到審稿影片（可能已被移動或刪除），只能看文字與標註說明。</span></div>`;
  }
  return html`<div class="rv-frame-wrap">
    <div class="rv-frame" style=${`aspect-ratio:${w} / ${h}`}>
      <video ref=${ref} src=${src} preload="auto" muted playsinline controls onError=${() => setError(true)}
        onLoadedMetadata=${(e) => {
          const v = e.currentTarget;
          if (v.videoWidth && v.videoHeight) setDims([v.videoWidth, v.videoHeight]);
          seek();
        }}
        onSeeked=${(e) => setOnFrame(Math.abs(e.currentTarget.currentTime - t) < 0.08)}
        onPlay=${() => setOnFrame(false)} />
      ${marks.length > 0 && onFrame && html`<svg class="rv-ink" viewBox=${`0 0 ${w} ${h}`}>
        <${Marks} marks=${marks} w=${w} h=${h} />
      </svg>`}
    </div>
    ${!onFrame && html`<${Button} size="sm" icon="retry" onClick=${seek}>回到意見這一格<//>`}
  </div>`;
}

function Attachment({ url, insert }) {
  if (!insert.file) {
    return html`<div class="note note-warn"><span class="note-text">附件遺失或格式不正確（${insert.name || "未命名"}），請客戶重新傳送這個檔案。</span></div>`;
  }
  return html`<div class="rv-attach">
    <div class="rv-attach-media">
      ${insert.type === "video" ? html`<video src=${url} controls preload="metadata" playsinline />` : html`<img src=${url} alt=${insert.name} />`}
    </div>
    <div class="rv-attach-meta">
      <b>${insert.name}</b>
      <span class="dim">插入一個 ${insert.duration} 秒的鏡頭</span>
    </div>
  </div>`;
}

function MusicFile({ url, music }) {
  if (!music.file) {
    return html`<div class="note note-warn"><span class="note-text">配樂檔遺失或格式不正確（${music.name || "未命名"}），請客戶重新傳送。</span></div>`;
  }
  return html`<div class="rv-attach">
    <div class="rv-attach-meta">
      <b>${music.name}</b>
      <audio src=${url} controls preload="metadata" />
      <span>${music.volume == null ? "客戶沒調音量，套用時沿用原本配樂的音量"
        : `客戶要的音量：${Math.round(music.volume * 100)}%（在審稿頁試聽調好的，套用時照這個音量）`}</span>
      <span class="dim">套用時換掉原本的配樂：有標「背景音樂」的音軌只換那幾軌，沒標就換掉所有音樂軌，配音與分離的原聲不動。
        新配樂從 0 秒開始，比影片長的部分在片尾截斷並淡出。</span>
    </div>
  </div>`;
}

function Detail({ base, rid, item, busy, onDecide, onExtra, onApply }) {
  const status = STATUS[item.status];
  const locked = item.status === "done" || busy;
  const file = item.kind === "insert" || item.kind === "music" ? (item[item.kind] || {}).file : "";
  const noFile = (item.kind === "insert" || item.kind === "music") && !file;
  const fileUrl = `${base}/${enc(rid)}/file?path=${enc(file || "")}`;
  const music = item.kind === "music";
  return html`<div class="rv-detail">
    <div class="rv-detail-head">
      <b>${where(item)}</b>
      <${Badge} tone=${item.kind === "insert" || music ? "accent" : "neutral"}>${kindLabel(item)}<//>
      ${status && html`<${Badge} tone=${status[0]}>${status[1]}<//>`}
    </div>
    ${item.kind !== "general" && !music && html`<${Frame} key=${rid} src=${`${base}/${enc(rid)}/video`} t=${item.t} marks=${item.marks} />`}
    ${item.kind === "insert" && html`<${Attachment} url=${fileUrl} insert=${item.insert} />`}
    ${music && html`<${MusicFile} url=${fileUrl} music=${item.music} />`}
    <div class="rv-said">
      <span class="dim">${music ? "客戶的配樂說明（只給你看，套用時不會讀）" : "客戶原話"}</span>
      <p>${item.text || (item.marks.length ? "（沒有文字，只有畫面標註）" : music ? "（沒有說明）" : "（沒有文字）")}</p>
    </div>
    <div class="rv-decide">
      <${Segmented} value=${item.decision} disabled=${locked}
        options=${DECISIONS.map((d) => ({ ...d, disabled: (noFile && d.value !== "skip") || (music && d.value === "tweak") }))}
        onChange=${onDecide} />
      ${accepted(item) && html`<${Button} size="sm" icon="sparkles" busy=${busy === item.id} disabled=${!!busy}
        onClick=${onApply}>套用這條<//>`}
    </div>
    ${item.decision === "tweak" && html`<label class="rv-extra">
      <span>補充指示（AI 會照客戶意見，加上這段做法修改）</span>
      <${DraftText} multiline rows=${2} value=${item.extra} onCommit=${onExtra} disabled=${locked}
        placeholder=${item.kind === "insert" ? "插入素材照固定流程處理，補充指示不會用到" : "例如：LOGO 放大到 1.5 倍，位置不變"} />
    </label>`}
    ${item.result && html`<div class=${`note note-${status ? status[0] : "neutral"}`}>
      <span class="note-text">${item.result}${item.applied ? html`<span class="dim">（${fmtDateTime(item.applied)}）</span>` : ""}</span>
    </div>`}
  </div>`;
}

function RevisionsBody({ pid, close }) {
  const base = `/api/projects/${enc(pid)}/revisions`;
  const [rounds, setRounds] = useState(null);
  const [rid, setRid] = useState(null);
  const [round, setRound] = useState(null);
  const [cid, setCid] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState("");
  const [busy, setBusy] = useState(null); // the id of the note being applied
  const [run, setRun] = useState(null); // {i, n} while applying the accepted notes
  const stop = useRef(false);
  useEffect(() => () => { stop.current = true; }, []); // closing the dialog stops after the current note

  const refresh = (pick = null) => api.get(base).then(({ rounds: list }) => {
    setRounds(list);
    setRid((cur) => pick || (list.some((r) => r.id === cur) ? cur : list[0] ? list[0].id : null));
  }, (err) => setError(err.message));
  useEffect(() => { refresh(); }, [pid]);
  useEffect(() => {
    setRound(null);
    if (!rid) return;
    api.get(`${base}/${enc(rid)}`).then(({ round: r }) => {
      setRound(r);
      setCid(r.items[0] ? r.items[0].id : null);
    }, (err) => toastError(err, "讀不到這一輪修改意見"));
  }, [rid]);

  const setItem = (item) => setRound((r) => r && { ...r, items: r.items.map((i) => (i.id === item.id ? item : i)) });
  const itemUrl = (item) => `${base}/${enc(rid)}/items/${enc(item.id)}`;
  const patch = async (item, body) => {
    try {
      setItem((await request("PATCH", itemUrl(item), body)).item);
    } catch (err) {
      toastError(err, "無法儲存");
    }
  };
  const apply = async (item) => {
    setBusy(item.id);
    try {
      const res = await api.post(`${itemUrl(item)}/apply`);
      setItem(res.item);
      return res.item;
    } catch (err) {
      toastError(err, "無法套用這條意見");
      return null;
    } finally {
      setBusy(null);
    }
  };

  // The copy takes the round along (store.duplicate copies revisions/), and this project drops it, so the
  // original keeps no trace of the notes.
  const moveToCopy = async (r) => {
    const name = `${getState().project.name}（${r.by || "客戶"}修改）`;
    let project;
    try {
      ({ project } = await api.post(`/api/projects/${enc(pid)}/duplicate`, { name }));
    } catch (err) {
      toastError(err, "無法建立副本，修改意見留在這個專案");
      return false;
    }
    try {
      await api.del(`${base}/${enc(r.id)}`);
    } catch (err) {
      toastError(err, "副本已建立，但沒能從原專案移除這一輪修改意見");
    }
    close(null);
    if (await openProject(project.id, getState().view)) {
      toast(`已開啟副本「${project.name}」，修改意見會套用在這裡`, { level: "success" });
      openRevisions();
    }
    return true;
  };
  const loaded = async (r) => {
    if (!r) return;
    toast(`已載入 ${r.by || "客戶"} 的修改意見，共 ${r.items.length} 條`, { level: "success" });
    const copy = await confirmDialog({
      title: "要複製成新專案再修改嗎？", confirmLabel: "複製新專案", cancelLabel: "直接改這個專案",
      message: `選「複製新專案」會把「${getState().project.name}」複製一份並開啟，修改意見只套用在副本，原版本保持不變。`,
    });
    if (copy && await moveToCopy(r)) return;
    refresh(r.id);
  };
  const loadZip = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".zip,application/zip";
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      setLoading(`正在上傳 ${file.name}…`);
      try {
        loaded((await upload(`${base}?name=${enc(file.name)}`, file, (p) => {
          setLoading(p < 1 ? `正在上傳 ${file.name}（${Math.round(p * 100)}%）…` : `正在讀取 ${file.name}…`);
        })).round);
      } catch (err) {
        toastError(err, "無法載入修改意見");
      } finally {
        setLoading("");
      }
    };
    input.click();
  };
  const pickFolder = async () => {
    setLoading("請在跳出的視窗選擇資料夾…");
    try {
      loaded((await api.post(`${base}/pick`)).round);
    } catch (err) {
      toastError(err, "無法載入修改意見");
    } finally {
      setLoading("");
    }
  };
  const remove = async () => {
    const r = rounds.find((x) => x.id === rid);
    const ok = await confirmDialog({
      title: "移除這一輪修改意見？", danger: true, confirmLabel: "移除",
      message: `${r.by || "客戶"} 的 ${r.count} 條意見與附件會從專案移除，已套用的修改不受影響（要退回請用復原）。`,
    });
    if (!ok) return;
    try {
      await api.del(`${base}/${enc(rid)}`);
      setRid(null);
      refresh();
    } catch (err) {
      toastError(err, "無法移除");
    }
  };

  // Later notes first, so a picture cut in early in the film never moves the place of one after it; then the
  // soundtrack, and the overall note last, on the edited film.
  const applyAll = async () => {
    const order = (i) => i.t ?? (i.kind === "music" ? -1 : -2);
    const queue = round.items.filter(accepted).sort((a, b) => order(b) - order(a));
    if (!queue.length) return;
    stop.current = false;
    const out = [];
    for (let i = 0; i < queue.length && !stop.current; i += 1) {
      setRun({ i: i + 1, n: queue.length });
      setCid(queue[i].id);
      out.push(await apply(queue[i]));
    }
    setRun(null);
    const count = (s) => out.filter((x) => x && x.status === s).length;
    const parts = [`${count("done")} 條已套用`, count("manual") && `${count("manual")} 條需人工處理`,
      count("failed") && `${count("failed")} 條失敗`, out.length < queue.length && `${queue.length - out.length} 條沒做（已停止）`];
    toast(`修改意見：${parts.filter(Boolean).join("、")}`, { level: count("done") === queue.length ? "success" : "warn", duration: 8000 });
    refresh();
  };

  const loaders = html`<${Button} icon="upload" disabled=${!!loading || !!run} onClick=${loadZip}>載入 ZIP…<//>
    <${Button} icon="folder-open" disabled=${!!loading || !!run} onClick=${pickFolder}>選資料夾…<//>`;
  const items = round ? round.items : [];
  const item = items.find((i) => i.id === cid);
  const todo = items.filter(accepted).length;
  const undecided = items.filter((i) => !i.decision && i.status !== "done").length;

  let body;
  if (error) {
    body = html`<div class="note note-danger"><span class="note-text"><b>讀不到修改意見</b>：${error}</span></div>`;
  } else if (!rounds) {
    body = html`<div class="hist-loading"><${Spinner} /><span class="dim">讀取中…</span></div>`;
  } else if (!rounds.length) {
    body = html`<${Empty} icon="message" title="還沒有載入修改意見" actions=${loaders}>
      客戶在審稿頁按「打包回傳」會得到一個 ZIP 檔，直接載入；已經解壓縮的話，選那個資料夾。<//>`;
  } else {
    body = html`<div class="rv-bar">
      <${Select} class="rv-round" value=${rid} onChange=${setRid} disabled=${!!run}
        options=${rounds.map((r) => ({ value: r.id,
          label: `${r.by || "客戶"}，${fmtDateTime(r.loaded)} 載入（${r.done}/${r.count} 已套用）` }))} />
      ${loaders}
      <${Button} kind="ghost" icon="trash" disabled=${!rid || !!run} onClick=${remove}>移除這一輪<//>
    </div>
    ${round && !round.pack && html`<div class="note note-warn"><span class="note-text">
      這份意見不是從這個專案的審稿包來的（可能是副本或審稿包已刪除），看不到審稿影片，時間點以鏡頭為準。</span></div>`}
    ${!round ? html`<div class="hist-loading"><${Spinner} /></div>` : html`<div class="rv-main">
      <ol class="rv-list">
        ${items.map((i) => {
          const status = STATUS[i.status];
          return html`<li key=${i.id}><button type="button" class=${`rv-row${i.id === cid ? " is-on" : ""}`}
              onClick=${() => setCid(i.id)}>
            <span class="rv-row-head">
              <b>${i.kind === "general" ? "整體" : i.kind === "music" ? "配樂" : fmtTime(i.t)}</b>
              <span class="dim">${kindLabel(i)}</span>
              ${status ? html`<${Badge} tone=${status[0]}>${status[1]}<//>`
                : i.decision && html`<${Badge} tone=${i.decision === "skip" ? "neutral" : "accent"}>${DECIDED[i.decision]}<//>`}
            </span>
            <span class="rv-row-text">${i.kind === "music" ? (i.music.name || "（未命名）")
              + (i.music.volume == null ? "" : `，音量 ${Math.round(i.music.volume * 100)}%`) : i.text || "（只有標註）"}</span>
          </button></li>`;
        })}
      </ol>
      ${item ? html`<${Detail} base=${base} rid=${rid} item=${item} busy=${busy}
        onDecide=${(decision) => patch(item, { decision })} onExtra=${(extra) => patch(item, { extra })}
        onApply=${() => apply(item)} />` : html`<div></div>`}
    </div>`}`;
  }

  const footer = html`${loading ? html`<span class="modal-foot-note dim"><${Spinner} size=${13} /> ${loading}</span>`
      : run ? html`<span class="modal-foot-note rv-run"><${Progress} value=${(run.i - 1) / run.n} />
        正在套用第 ${run.i} / ${run.n} 條（文字意見交給 AI 助理，每條可能要等一會兒）</span>`
      : html`<span class="modal-foot-note dim">${round ? `${undecided} 條還沒決定，${todo} 條待套用` : ""}</span>`}
    ${run ? html`<${Button} icon="stop" onClick=${() => { stop.current = true; }}>做完這條就停止<//>`
      : round && html`<${Button} kind="primary" icon="sparkles" disabled=${!todo || !!busy} onClick=${applyAll}>
        套用勾選的意見${todo ? `（${todo}）` : ""}<//>`}
    <${Button} onClick=${() => close(null)}>關閉<//>`;
  return html`<${Modal} title=${`修改意見：${(getState().project || {}).name || ""}`} width=${1080} footer=${footer}
      onClose=${() => close(null)} backdropClose=${false} class="revisions-dialog">
    ${body}
  <//>`;
}

export function openRevisions() {
  const { pid } = getState();
  if (!pid) return Promise.resolve(null);
  return openModal((close) => html`<${RevisionsBody} pid=${pid} close=${close} />`);
}
