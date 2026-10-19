// Draft state for settings blocks with text or number fields: edits stay local until saved, and a
// change arriving from the server (another window, a normalised value) replaces an untouched draft.
import { html, useState, useEffect } from "../lib/h.js";
import { saveSettings } from "../lib/actions.js";
import { Button } from "../components/ui.js";
import { toast } from "../components/toast.js";

export function useSectionDraft(saved, toPatch, doneText) {
  const savedKey = JSON.stringify(saved);
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState(false);
  useEffect(() => setDraft(saved), [savedKey]);

  const save = async () => {
    setBusy(true);
    const result = await saveSettings(toPatch(draft));
    setBusy(false);
    if (result) toast(doneText, { level: "success" });
    return result;
  };
  return {
    draft,
    busy,
    dirty: JSON.stringify(draft) !== savedKey,
    set: (patch) => setDraft((d) => ({ ...d, ...patch })),
    revert: () => setDraft(saved),
    save,
  };
}

export function DraftFoot({ form, invalid = false, onSave = null, children = null }) {
  return html`<footer class="eng-foot">
    ${form.dirty && html`<span class="eng-dirty">有未儲存的修改</span>`}
    <span class="grow"></span>
    ${children}
    ${form.dirty && html`<${Button} size="sm" kind="ghost" icon="undo" onClick=${form.revert}>還原<//>`}
    <${Button} size="sm" kind="primary" icon="save" busy=${form.busy} disabled=${!form.dirty || invalid || form.busy}
      onClick=${onSave || form.save}>儲存<//>
  </footer>`;
}
