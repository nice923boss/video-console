// p5 text (vc/p5src.py, vc/p5render.py): a clip made from a p5 animation page lists the Chinese texts of its
// segment; edit them here and "重算" draws the segment again in a headless browser and swaps the clip's file
// for the new transparent WebM (one undo step). The user's p5 files are only read, never written.
import { html, useState, useEffect } from "../lib/h.js";
import { Field, Button, TextArea, TextInput } from "./ui.js";
import { Icon } from "./icons.js";
import { useStore } from "../lib/store.js";
import { api } from "../lib/api.js";
import { submitJob } from "../lib/jobs-actions.js";
import { toastError } from "./toast.js";

const enc = encodeURIComponent;
const OLD_APP = "這個功能要重新啟動 Video Console 才能使用";
const HINT = "p5 動畫輸出的透明影片可以連結原始碼頁面，在這裡改字後按「重算」：會用瀏覽器重新畫這段動畫（要能上網載入 p5.js），完成後自動換上新影片，可以復原。原始碼檔案不會被修改";
// `${pid}:${mediaId}` -> {at: text}: edits not yet rendered, kept while the inspector shows other clips.
const drafts = new Map();

const active = (j) => j.status === "queued" || j.status === "running";
const message = (err) => (err.status === 405 || (err.status === 404 && err.message === "找不到這個 API") ? OLD_APP : err.message);

// Drop drafts the source no longer has, or that the rendered clip already shows.
function prune(key, info) {
  const now = new Map((info.texts || []).map((t) => [String(t.at), t.text]));
  const kept = Object.entries(drafts.get(key) || {}).filter(([at, v]) => now.has(at) && now.get(at) !== v);
  if (kept.length) drafts.set(key, Object.fromEntries(kept));
  else drafts.delete(key);
}

export function P5TextField({ media }) {
  const { pid, jobs } = useStore((s) => ({ pid: s.pid, jobs: s.jobs }));
  const key = `${pid}:${media.id}`;
  const base = `/api/projects/${enc(pid)}/media/${enc(media.id)}/p5`;
  const [info, setInfo] = useState(null);
  const [error, setError] = useState("");
  const [form, setForm] = useState(null); // {page, segment} while choosing the source
  const [sending, setSending] = useState(false);
  const [reload, setReload] = useState(0);
  const [, redraw] = useState(0);
  const job = jobs.find((j) => j.pid === pid && j.kind === "p5render" && j.payload && j.payload.media === media.id && active(j));
  // A finished render changes the file and the stored edits; read the texts again then.
  const stamp = `${media.file}|${JSON.stringify(media.p5 || null)}|${reload}`;

  useEffect(() => {
    let live = true;
    api.get(base).then((r) => {
      if (!live) return;
      prune(key, r);
      setInfo(r);
      setError("");
    }).catch((err) => live && (setInfo(null), setError(message(err))));
    return () => { live = false; };
  }, [base, stamp]);

  if (!info) {
    return error ? html`<${Field} label="p5 文字"><div class="note note-warn"><${Icon} name="alert" size=${15} />
      <span class="note-text">${error}</span></div><//>` : null;
  }

  const mine = drafts.get(key) || {};
  const textOf = (t) => (String(t.at) in mine ? mine[String(t.at)] : t.text);
  const setText = (t, v) => {
    const next = { ...mine, [String(t.at)]: v };
    if (v === t.text) delete next[String(t.at)];
    if (Object.keys(next).length) drafts.set(key, next);
    else drafts.delete(key);
    redraw((n) => n + 1);
  };
  const dirty = Object.keys(mine).length > 0;

  const pick = async () => {
    try {
      const r = await api.post(`/api/projects/${enc(pid)}/pick-p5-page`, { start: (form && form.page) || info.page || "" });
      if (r && r.page) setForm((f) => ({ ...(f || {}), page: r.page }));
    } catch (err) {
      toastError(err, "無法開啟選檔視窗");
    }
  };
  const link = async (page, segment) => {
    setSending(true);
    try {
      const r = await api.post(`${base}/link`, { page, segment });
      setForm(null);
      if (r.linked) {
        prune(key, r);
        setInfo(r);
      } else {
        drafts.delete(key);
        setReload((n) => n + 1);
      }
    } catch (err) {
      toastError(err, page ? "無法連結 p5 原始碼" : "無法取消連結");
    } finally {
      setSending(false);
    }
  };
  const render = async () => {
    const texts = info.texts.map((t) => ({ at: t.at, text: textOf(t) }));
    setSending(true);
    try {
      await submitJob("p5render", null, { media: media.id, texts });
    } finally {
      setSending(false);
    }
  };

  if (form || !info.linked) {
    if (!form) {
      return html`<${Field} label="p5 文字" hint=${HINT}>
        <${Button} size="sm" icon="link" onClick=${() => setForm({ page: info.page || "", segment: info.segment || "" })}>
          連結 p5 原始碼…<//>
      <//>`;
    }
    return html`<${Field} label="連結 p5 原始碼"
        hint="選 p5 動畫的頁面（通常是 anim\\index.html），片段代號是 SCENES 裡的名稱，例如 M04">
      <div class="row">
        <${TextInput} class="grow" mono value=${form.page} placeholder="F:\\…\\anim\\index.html"
          onChange=${(v) => setForm({ ...form, page: v })} />
        <${Button} size="sm" onClick=${pick}>選擇…<//>
      </div>
      <div class="row">
        <${TextInput} class="grow" mono value=${form.segment} placeholder="片段代號，例如 M04" maxlength="40"
          onChange=${(v) => setForm({ ...form, segment: v })} />
        <${Button} size="sm" kind="primary" busy=${sending} disabled=${!form.page.trim() || !form.segment.trim()}
          onClick=${() => link(form.page.trim(), form.segment.trim())}>連結<//>
        <${Button} size="sm" kind="ghost" onClick=${() => setForm(null)}>取消<//>
      </div>
    <//>`;
  }

  const running = job && job.status === "running";
  const label = !job ? "重算" : running ? `重算中 ${Math.round((job.progress || 0) * 100)}%` : "排隊中";
  const where = info.file ? `${info.file} 第 ${info.line} 行` : info.page;
  return html`<${Field} label=${`p5 文字（${info.segment}）`} hint=${HINT}
      after=${html`<${Button} size="sm" kind="ghost" title="重新讀取原始碼裡的文字" onClick=${() => setReload((n) => n + 1)}>重新讀取<//>
        <${Button} size="sm" kind="ghost" onClick=${() => setForm({ page: info.page, segment: info.segment })}>改連結<//>
        <${Button} size="sm" kind="ghost" disabled=${!!job} onClick=${() => link("", "")}>取消連結<//>`}>
    <div class="src-info dim"><${Icon} name="code" size=${14} /><span class="grow mono" title=${info.page}>${where}</span></div>
    ${info.error && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
      <span class="note-text">${info.error}。原始碼搬移或改名時，按「改連結」重新指定頁面</span></div>`}
    ${(info.stale || []).length > 0 && html`<div class="note note-warn"><${Icon} name="alert" size=${15} />
      <span class="note-text">原始碼改過，之前改的 ${info.stale.map((s) => `「${s}」`).join("、")} 找不到了，下次重算不會套用</span></div>`}
    ${!info.error && info.texts.length === 0 && html`<div class="dim">這個片段的程式裡沒有中文字串</div>`}
    ${info.texts.map((t) => html`<${Field} label=${`第 ${t.line} 行`}
        hint=${t.edited ? html`原文：${t.original}` : null}
        after=${(t.edited || textOf(t) !== t.text) && html`<${Button} size="sm" kind="ghost" disabled=${!!job}
          title=${t.edited ? "改回原始碼的文字（按重算後生效）" : "捨棄這一行還沒重算的修改"}
          onClick=${() => setText(t, textOf(t) !== t.text ? t.text : t.original)}>${textOf(t) !== t.text ? "捨棄" : "還原"}<//>`}>
      <${TextArea} rows=${1} autosize value=${textOf(t)} disabled=${!!job} maxlength="1000"
        onChange=${(v) => setText(t, v)} />
    <//>`)}
    ${info.texts.length > 0 && html`<div class="row">
      <${Button} kind="primary" size="sm" icon="refresh" busy=${sending} disabled=${!!job || !dirty} onClick=${render}
        title=${dirty ? "用修改後的文字重新畫這段動畫，完成後換上新影片" : "先修改上面的文字"}>${label}<//>
      ${dirty && !job && html`<${Button} size="sm" kind="ghost" onClick=${() => { drafts.delete(key); redraw((n) => n + 1); }}>
        全部捨棄<//>`}
    </div>`}
  <//>`;
}
