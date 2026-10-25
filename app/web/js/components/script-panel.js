// The storyboard as one editable script (SB-14). The server renders the text from the shots, so an edit made
// on a shot shows here at once while the text is not being edited. Applying an edit writes back only the
// fields that differ from the text it started from, so a shot edited meanwhile keeps that edit.
import { html, useState, useEffect } from "../lib/h.js";
import { useStore, getState } from "../lib/store.js";
import { api } from "../lib/api.js";
import { loadProject, focusShot, LINK_PREF } from "../lib/actions.js";
import { readPref } from "../lib/prefs.js";
import { Icon } from "./icons.js";
import { Button, TextArea } from "./ui.js";
import { toast, toastError } from "./toast.js";

const enc = encodeURIComponent;
const isBool = (v) => typeof v === "boolean";
// The draft outlives the panel (switching to the cards or another view and back), for its project only.
let kept = { pid: null, draft: null };

// The number of the "#N" header above the caret; 0 for a new block or none.
function blockAt(text, caret) {
  const lines = text.slice(0, caret).split("\n");
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const m = /^#(\d*)/.exec(lines[i]);
    if (m) return Number(m[1] || 0);
  }
  return 0;
}

function summary(out) {
  const parts = [out.updated && `更新 ${out.updated} 個鏡頭`, out.added && `新增 ${out.added} 個鏡頭`,
    out.captions && `字幕變動 ${out.captions} 則`].filter(Boolean);
  return parts.length ? `已${parts.join("、")}` : "";
}

export function ScriptPanel() {
  const { pid, rev } = useStore((s) => ({ pid: s.pid, rev: s.project && s.project.rev }));
  const [script, setScript] = useState(null); // {text, ids, rev} as the server rendered it
  const [draft, setDraft] = useState(() => (kept.pid === pid ? kept.draft : null)); // {text, base}; null: unedited
  const [busy, setBusy] = useState(false);
  const editing = draft !== null;

  useEffect(() => {
    kept = { pid, draft };
  }, [pid, draft]);

  useEffect(() => {
    if (editing) return;
    api.get(`/api/projects/${enc(pid)}/script`).then(
      (data) => setScript((cur) => (cur && cur.rev > data.rev ? cur : data)),
      (err) => toastError(err, "無法讀取劇本"));
  }, [pid, rev, editing]);

  const edit = (text) => setDraft((d) => {
    const base = d ? d.base : script;
    return text === base.text ? null : { text, base };
  });

  const apply = async () => {
    if (!draft || busy) return;
    setBusy(true);
    try {
      const out = await api.post(`/api/projects/${enc(pid)}/script`, {
        text: draft.text, base: draft.base.text, ids: draft.base.ids, link: readPref(LINK_PREF, true, isBool),
      });
      if (out.changed) await loadProject(pid);
      setDraft(null);
      toast(summary(out) || "劇本沒有變更", { level: out.changed ? "success" : "info" });
      for (const note of out.notes || []) toast(note, { level: "warn" });
    } catch (err) {
      toastError(err, "無法套用劇本");
    } finally {
      setBusy(false);
    }
  };

  // The caret's block shows its shot in the inspector.
  const follow = (e) => {
    const ids = (draft ? draft.base : script || {}).ids || [];
    const id = ids[blockAt(e.currentTarget.value, e.currentTarget.selectionStart) - 1];
    const s = getState();
    if (id && id !== s.focus && s.project && s.project.shots.some((x) => x.id === id)) focusShot(id);
  };

  const keyDown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      apply();
    }
  };

  const stale = editing && rev !== draft.base.rev;
  const text = draft ? draft.text : script ? script.text : "";
  return html`<div class="script-panel">
    <div class="script-bar">
      <span class=${editing ? "script-state dirty" : "script-state dim"}>${editing ? "已修改，尚未套用" : "與分鏡同步"}</span>
      ${stale && html`<span class="script-stale"><${Icon} name="alert" size=${13} />分鏡在你編輯時有其他修改，套用時只寫回劇本裡改過的欄位</span>`}
      <span class="grow"></span>
      <${Button} size="sm" icon="undo" disabled=${!editing || busy} onClick=${() => setDraft(null)}>捨棄修改<//>
      <${Button} size="sm" kind="primary" icon="check" disabled=${!editing || busy} title="Ctrl + Enter"
        onClick=${apply}>套用到分鏡<//>
    </div>
    <${TextArea} class="script-text" mono value=${text} disabled=${!script && !draft} spellcheck=${false}
      aria-label="劇本" onChange=${edit} onClick=${follow} onKeyUp=${follow} onKeyDown=${keyDown} />
    <div class="script-hint dim">
      <span>「#數字」是原有鏡頭，「#」加名稱是新鏡頭</span><i></i><span>描述、動作、運鏡、字幕、片長、備註</span><i></i>
      <span>續行縮排兩格</span><i></i><span>刪掉段落不會刪鏡頭，刪除與排序請用卡片</span>
    </div>
  </div>`;
}
