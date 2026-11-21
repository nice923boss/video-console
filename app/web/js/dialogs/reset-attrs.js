// 重設屬性 (edit.resetAttrs): the settings of the selected clips that are off their defaults (lib/reset-attrs.js),
// all ticked when the dialog opens; the ticked ones go back to their defaults in one undo step.
import { html, useState } from "../lib/h.js";
import { ops } from "../lib/actions.js";
import { useDialogKeys, undoKeyText } from "../lib/keymap.js";
import { adjusted, grouped, resetOps } from "../lib/reset-attrs.js";
import { current, together } from "../components/tl-commands.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button } from "../components/ui.js";
import { toast } from "../components/toast.js";

function ResetAttrs({ groups, count, close }) {
  const [picked, setPicked] = useState(() => new Set(groups.map((g) => g.key)));
  const [busy, setBusy] = useState(false);
  const all = picked.size === groups.length;
  const toggle = (key, on) => setPicked((prev) => {
    const next = new Set(prev);
    if (on) next.add(key);
    else next.delete(key);
    return next;
  });

  const submit = async () => {
    if (!picked.size || busy) return;
    setBusy(true);
    const res = await ops(resetOps(groups.filter((g) => picked.has(g.key)).flatMap((g) => g.rows)), { label: "重設屬性" });
    setBusy(false);
    if (!res) return;
    toast(`已把 ${picked.size} 項屬性恢復成預設值，${undoKeyText()}一次還原`, { id: "tl-reset", level: "success" });
    close(res);
  };
  useDialogKeys({ "dialog.confirm": { fn: submit, allowInInput: true } });

  const footer = html`
    <${Button} onClick=${() => close(null)}>取消<//>
    <${Button} kind="primary" icon="undo" busy=${busy} disabled=${!picked.size} title=${picked.size ? undefined : "先勾選要重設的屬性"}
      onClick=${submit} data-autofocus>${picked.size ? `重設 ${picked.size} 項` : "重設"}<//>`;

  return html`<${Modal} title="重設屬性" width=${460} backdropClose=${false} onClose=${() => close(null)} footer=${footer}>
    <div class="bx-head">
      <span class="dim">${count > 1 ? `選取的 ${count} 個片段` : "這個片段"}調整過的屬性，勾選的會恢復成預設值。</span>
      <span class="grow"></span>
      <${Button} size="sm" kind="ghost" onClick=${() => setPicked(all ? new Set() : new Set(groups.map((g) => g.key)))}>
        ${all ? "全部取消" : "全選"}<//>
    </div>
    <div class="bx-list">
      ${groups.map((g) => html`<label key=${g.key} class="bx-row">
        <input type="checkbox" checked=${picked.has(g.key)} onChange=${(e) => toggle(g.key, e.currentTarget.checked)} />
        <span class="grow bx-name">${g.label}</span>
        <span class="dim">${g.detail}</span>
      </label>`)}
    </div>
  <//>`;
}

let open = false;

// The selected clips (all of a Shift or Ctrl selection), else the shot under the playhead. A second press before the
// dialog has taken the keys (it does on its first render) must not open another one.
export function resetAttrs(project, clips, sel) {
  if (open) return Promise.resolve(null);
  const cur = current(project, clips, sel);
  if (!cur) return toast("先選取要重設屬性的片段", { id: "tl-reset", level: "warn" });
  const items = together(project, sel, cur.item);
  const groups = grouped(items.flatMap((x) => adjusted(project, clips, cur.type, x)));
  if (!groups.length) {
    return toast(`${items.length > 1 ? `選取的 ${items.length} 個片段` : "這個片段"}沒有調整過的屬性`, { id: "tl-reset", duration: 2500 });
  }
  open = true;
  return openModal((close) => html`<${ResetAttrs} groups=${groups} count=${items.length} close=${close} />`)
    .finally(() => { open = false; });
}
