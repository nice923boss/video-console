// Keyframes in the edit properties panels: a keyed field's slider edits its key at the playhead (adding
// one there), and the list of the clip's keys can jump to or delete each one. The list opens with
// edit.keyframePanel (lib/keymap.js).
import { html, useState, useEffect, useRef } from "../lib/h.js";
import { setState, useStore } from "../lib/store.js";
import { clock, setTime, onClock } from "../lib/clock.js";
import { clamp, fmtSeconds } from "../lib/format.js";
import { hasKeys, valueAt, withKey } from "../lib/keyframes.js";
import { keyHint, keyLabel } from "../lib/keymap.js";
import { Button, IconButton } from "./ui.js";

const FIELD_NAMES = { x: "水平", y: "垂直", w: "大小", opacity: "不透明度", volume: "音量", grade: "調色" };

const fieldsOf = (k) => Object.keys(k).filter((f) => f !== "t");

// The playhead in seconds into a clip, held to the clip. Follows the playhead while paused when `live`.
export function useClipTime(start, length, live) {
  const [t, setT] = useState(clock.time);
  useEffect(() => (live ? onClock((c) => { if (!c.playing) setT(c.time); }) : undefined), [live]);
  return clamp((live ? t : clock.time) - start, 0, length);
}

// An ops patch setting `values` at `local` seconds into the clip: fields with keyframes change (or gain)
// their key there, the others change for the whole clip.
export function keyedPatch(item, local, values) {
  const keyed = Object.fromEntries(Object.entries(values).filter(([f]) => hasKeys(item, f)));
  const plain = Object.fromEntries(Object.entries(values).filter(([f]) => !hasKeys(item, f)));
  return Object.keys(keyed).length ? { ...plain, keyframes: withKey(item, local, keyed) } : plain;
}

export function KeyList({ item, start, length, local, update, onAdd }) {
  const open = useStore((s) => s.keyPanel);
  const ref = useRef(null);
  useEffect(() => {
    if (open && ref.current) ref.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [open]);
  const keys = item.keyframes || [];
  // A field losing its last key keeps that key's value.
  const remove = (k) => {
    const rest = keys.filter((x) => x !== k);
    const kept = Object.fromEntries(fieldsOf(k).filter((f) => !rest.some((x) => f in x)).map((f) => [f, k[f]]));
    update({ ...kept, keyframes: rest }, "刪除關鍵幀");
  };
  const clearAll = () => {
    const fields = [...new Set(keys.flatMap(fieldsOf))];
    update({ ...Object.fromEntries(fields.map((f) => [f, valueAt(item, f, local)])), keyframes: [] }, "清除關鍵幀");
  };
  const onToggle = (e) => {
    if (e.currentTarget.open !== open) setState({ keyPanel: e.currentTarget.open });
  };

  return html`<details class="grade-sec key-list" ref=${ref} open=${open} onToggle=${onToggle}>
    <summary>關鍵幀${keys.length ? `（${keys.length}）` : ""}<span class="dim">${keyHint("edit.keyframePanel")}</span></summary>
    <div class="grade-sec-body">
      ${keys.length
        ? html`<span class="field-hint">有關鍵幀的數值在關鍵幀之間平滑變化。拖曳上方滑桿會改寫播放頭位置的關鍵幀（沒有就新增一個）。</span>
          ${keys.map((k) => html`<div key=${k.t} class=${`key-row${Math.abs(k.t - local) < 1e-3 ? " is-here" : ""}`}>
            <button type="button" class="key-time mono" title="跳到這個關鍵幀" onClick=${() => setTime(start + k.t)}>
              ${fmtSeconds(start + k.t, 2)}</button>
            <span class="grow dim">${fieldsOf(k).map((f) => FIELD_NAMES[f] || f).join("、")}</span>
            <${IconButton} icon="trash" size=${13} title="刪除這個關鍵幀" onClick=${() => remove(k)} />
          </div>`)}`
        : html`<span class="field-hint">還沒有關鍵幀。${keyLabel("edit.keyframeAdd") ? `按 ${keyLabel("edit.keyframeAdd")} 在播放頭加入，或` : ""}Shift+點擊時間軸上的片段，在點的位置加入。</span>`}
      <div class="row">
        <${Button} size="sm" icon="plus" onClick=${onAdd}>在播放頭加入<//>
        ${keys.length > 0 && html`<${Button} size="sm" kind="ghost" icon="trash" onClick=${clearAll}
          title="移除全部關鍵幀，數值停在播放頭位置的值">全部清除<//>`}
      </div>
    </div>
  </details>`;
}
