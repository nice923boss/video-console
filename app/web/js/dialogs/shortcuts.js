// Keyboard shortcut reference ("?"): the keys each command has right now (lib/keymap.js, rebindable in
// Settings), dialog keys included.
import { html } from "../lib/h.js";
import { useStore } from "../lib/store.js";
import { GROUPS, allCommands, keysOf, readKeymap, selectKeymap } from "../lib/keymap.js";
import { openModal, Modal } from "../components/overlay.js";
import { Button, Keys } from "../components/ui.js";
import { openEditHelp } from "./edit-help.js";

function ShortcutList() {
  const km = readKeymap(useStore(selectKeymap));
  const commands = allCommands();
  const groups = GROUPS.map((title) => ({
    title,
    items: commands.filter((c) => c.group === title).map((c) => [keysOf(c.id, km), c.label]).filter(([keys]) => keys.length),
  })).filter((g) => g.items.length);
  return html`<div class="shortcuts">
    ${groups.map((g) => html`<section class="kb-group" key=${g.title}>
      <h4>${g.title}</h4>
      ${g.items.map(([combos, text]) => html`<div class="kb-row" key=${text}>
        <span class="kb-keys">${combos.map((c) => html`<${Keys} key=${c} combo=${c} />`)}</span>
        <span>${text}</span>
      </div>`)}
    </section>`)}
  </div>`;
}

let open = false;

export function openShortcuts() {
  if (open) return Promise.resolve(null);
  open = true;
  return openModal((close) => html`<${Modal} title="快捷鍵一覽" width=${760} onClose=${() => close(null)}
      footer=${html`<span class="modal-foot-note dim">輸入文字時，單鍵快捷鍵不會作用；到「設定」的「快捷鍵」可以改鍵</span>
        <${Button} icon="book" onClick=${() => { close(null); openEditHelp(); }}>剪輯功能說明<//>
        <${Button} kind="primary" onClick=${() => close(null)}>知道了<//>`}>
    <${ShortcutList} />
  <//>`).finally(() => { open = false; });
}
