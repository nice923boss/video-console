// The user's own style library (IM-07): styles saved from any project, applied to this one in one click.
// The sample picture only reminds what the look was; generation still receives the text alone.
import { html, useEffect, useState } from "../lib/h.js";
import { api } from "../lib/api.js";
import { updateProject } from "../lib/actions.js";
import { Button, IconButton, Field } from "./ui.js";
import { Thumb } from "./media.js";
import { promptDialog, confirmDialog } from "./overlay.js";
import { openRefPicker } from "./ref-picker.js";
import { toast, toastError } from "./toast.js";

export function StyleLibrary({ project }) {
  const [styles, setStyles] = useState([]);
  useEffect(() => {
    api.get("/api/styles").then((r) => setStyles(r.styles)).catch((err) => toastError(err, "讀不到風格庫"));
  }, []);
  const style = project.style || {};
  const ratio = `${project.settings.width}/${project.settings.height}`;

  const save = async () => {
    const name = await promptDialog({ title: "存進風格庫", label: "風格名稱", placeholder: "例如：港口晨光",
      confirmLabel: "下一步", hint: "會存下目前的風格描述與負向提示詞，所有專案都能套用" });
    if (!name) return;
    const item = await openRefPicker(project, { id: "" }, [], "選一張樣張（關閉視窗就不放樣張）");
    try {
      const r = await api.post("/api/styles", { name, prompt: style.prompt || "", negative: style.negative || "",
        pid: project.id, item: item || "" });
      setStyles(r.styles);
      toast(`已存進風格庫：${r.style.name}`, { level: "success" });
    } catch (err) {
      toastError(err, "存不進風格庫");
    }
  };
  const apply = (s) => updateProject({ style: { preset: "", prompt: s.prompt, negative: s.negative } },
    { label: `套用風格庫：${s.name}` });
  const remove = async (s) => {
    if (!(await confirmDialog({ title: `刪除「${s.name}」？`, message: "所有專案都不會再看到這筆風格；已套用的專案不受影響。",
      confirmLabel: "刪除", danger: true }))) return;
    try {
      setStyles((await api.del(`/api/styles/${s.id}`)).styles);
    } catch (err) {
      toastError(err, "刪不掉這筆風格");
    }
  };

  return html`<${Field} label="我的風格庫" hint="點一下就把風格描述與負向提示詞套到這個專案；樣張只是外觀參考，不會送去生成">
    ${styles.length > 0 && html`<div class="style-lib">
      ${styles.map((s) => html`<div class="style-lib-card" key=${s.id}>
        <button type="button" class="style-lib-pick" title=${s.prompt || s.negative} onClick=${() => apply(s)}>
          <${Thumb} src=${s.picture ? `/api/styles/${s.id}/picture` : ""} ratio=${ratio} icon="palette" />
          <span class="ref-cell-label">${s.name}</span>
        </button>
        <${IconButton} icon="x" size=${12} class="ref-extra-x" title="從風格庫刪除" onClick=${() => remove(s)} />
      </div>`)}
    </div>`}
    <${Button} size="sm" icon="plus" disabled=${!(style.prompt || style.negative)} onClick=${save}>把目前風格存進風格庫<//>
  <//>`;
}
