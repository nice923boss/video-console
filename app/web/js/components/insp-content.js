// Shot inspector, "描述" tab: what the shot shows (prompt, framing, camera move, action), its length
// and its label colour. The prompt and camera fields share one draft with their quick chips, so a
// chip click never overwrites text that is still being typed.
import { html, useState, useRef, useEffect } from "../lib/h.js";
import { useStore, getState } from "../lib/store.js";
import { useDialogKeys } from "../lib/keymap.js";
import { updateShot, navigate, ops } from "../lib/actions.js";
import { api } from "../lib/api.js";
import { setTime } from "../lib/clock.js";
import { hasCJK, newId, round, fmtSeconds } from "../lib/format.js";
import { videoTake, buildSequence, shotCaptions, castInText } from "../lib/sequence.js";
import { Field, TextArea, TextInput, useDraft, DraftText, NumberInput, Section, Chip, Button } from "./ui.js";
import { openMenu, openModal, Modal } from "./overlay.js";
import { toast, toastError } from "./toast.js";

const NO_COLOR = { value: null, label: "無顏色" };
// "None" plus the label colours set in settings (ui.labelColors).
export const labelColors = (settings) => [NO_COLOR, ...((settings && settings.ui && settings.ui.labelColors) || [])];

const NONE = [];
const LEAD_SEP = /^\s*[，,、]\s*/;
const GO_LLM = { label: "前往設定", fn: () => navigate("engines", { enginesTab: "llm" }) };

// value: a colour, null for none, undefined when a multi-selection disagrees (nothing highlighted).
export function ColorSwatches({ value, onChange }) {
  const colors = labelColors(useStore((s) => s.settings));
  return html`<div class="swatches" role="radiogroup" aria-label="標籤顏色">
    ${colors.map((c) => {
      const on = value === c.value;
      return html`<button type="button" key=${c.value || "none"} role="radio" aria-checked=${on ? "true" : "false"} title=${c.label}
        aria-label=${c.label} class=${`swatch ${on ? "active" : ""} ${c.value ? "" : "swatch-none"}`}
        style=${c.value ? `--swatch:${c.value}` : undefined} onClick=${() => !on && onChange(c.value)}></button>`;
    })}
  </div>`;
}

// The shot-size word a prompt starts with ("特寫，…" or "close-up, …") and the text after it.
function leadingSize(prompt, sizes) {
  const body = prompt.trimStart();
  const lower = body.toLowerCase();
  for (const s of sizes) {
    for (const word of [s.text, s.en]) {
      if (!word || !lower.startsWith(word.toLowerCase())) continue;
      const rest = body.slice(word.length);
      const sep = LEAD_SEP.exec(rest);
      if (rest && !sep) continue;
      return { size: s, rest: sep ? rest.slice(sep[0].length) : "" };
    }
  }
  return null;
}

// Chinese wording when the prompt is Chinese (or still empty), English otherwise.
const preferChinese = (text) => !text.trim() || hasCJK(text);

function EnhanceDialog({ before, after, close }) {
  const [text, setText] = useState(after);
  const apply = () => close(text.trim() ? text : null);
  useDialogKeys({ "dialog.confirm": { fn: apply, allowInInput: true } });
  return html`<${Modal} title="AI 潤飾結果" width=${680} backdropClose=${false} onClose=${() => close(null)}
      footer=${html`
        <${Button} onClick=${() => close(null)}>取消<//>
        <${Button} kind="primary" icon="check" onClick=${apply}>套用<//>`}>
    <div class="enhance-compare">
      <div class="enhance-col">
        <span class="field-label">原本的描述</span>
        <p class="enhance-before">${before}</p>
      </div>
      <div class="enhance-col">
        <span class="field-label">潤飾後（可以直接修改）</span>
        <${TextArea} value=${text} onChange=${setText} rows=${8} data-autofocus />
      </div>
    </div>
  <//>`;
}

function EnhanceButton({ text, kind, onApply }) {
  const [busy, setBusy] = useState(false);
  const llmOn = useStore((s) => !!(s.settings && s.settings.llm && s.settings.llm.enabled));
  const run = async (target) => {
    if (!text.trim()) {
      toast("請先輸入要潤飾的描述", { level: "warn" });
      return;
    }
    let res;
    setBusy(true);
    try {
      res = await api.post("/api/llm/enhance", { text, kind, target });
    } catch (err) {
      if (err && err.status === 409) toast(err.message, { level: "warn", title: "無法潤飾", action: GO_LLM });
      else toastError(err, "潤飾失敗");
      return;
    } finally {
      setBusy(false);
    }
    const next = await openModal((close) => html`<${EnhanceDialog} before=${text} after=${res.text || ""} close=${close} />`);
    if (next != null && next !== text) onApply(next);
  };
  const open = (e) => {
    if (!llmOn) {
      toast("尚未開啟文字模型，請到「引擎 › 文字模型」開啟", { level: "warn", action: GO_LLM });
      return;
    }
    openMenu(e, [
      { header: "用文字模型改寫" },
      { label: "潤飾（保持原語言）", icon: "wand", onClick: () => run("same") },
      { label: "潤飾並翻成英文", icon: "globe", onClick: () => run("en") },
    ]);
  };
  return html`<${Button} size="sm" kind="ghost" icon="wand" busy=${busy} title="用文字模型把描述改寫得更具體"
    onClick=${open}>潤飾<//>`;
}

const MIN_LINE = 0.5; // shortest caption "再加一句" makes, in seconds

// Where the next line of a shot goes: after its last bound caption up to the clip's end, or else the last one's
// second half. Returns the ops that make room (none, or shortening the last line) and the new line's times.
function nextLine(list, seq) {
  if (!list.length) return { room: [], start: seq.start, end: round(seq.start + seq.length, 3) };
  const last = list[list.length - 1];
  const clipEnd = round(seq.start + seq.length, 3);
  if (clipEnd - last.end >= MIN_LINE) return { room: [], start: last.end, end: clipEnd };
  if (last.end - last.start < 2 * MIN_LINE) return null;
  const mid = round((last.start + last.end) / 2, 3);
  return { room: [{ op: "caption.update", id: last.id, patch: { end: mid } }], start: mid, end: last.end };
}

// The captions bound to this shot, one box per line in time order (CP-06). Typing in the empty box adds a caption
// over the shot's clip (a further line after the last one, or in the last one's second half) and binds it;
// clearing a box removes that caption. Only the text is tied to the shot: timing stays each caption's own.
function CaptionSection({ project, shot }) {
  const caps = shotCaptions(project, shot.id);
  const clip = buildSequence(project).find((c) => c.shot.id === shot.id) || null;
  const [adding, setAdding] = useState(false);
  const created = useRef(null); // id of a caption added here that the project does not show yet
  const queue = useRef(Promise.resolve());
  const shown = created.current && caps.some((c) => c.id === created.current);
  useEffect(() => {
    if (!shown) return;
    created.current = null;
    setAdding(false);
  }, [shown]);
  useEffect(() => {
    created.current = null;
    setAdding(false);
  }, [shot.id]);
  // Boxes keep their place by index, so the empty box that becomes a caption keeps its focus.
  const lines = caps.length === 0 || (adding && !shown) ? [...caps, null] : caps;
  const commit = (i) => (v) => {
    queue.current = queue.current.then(async () => {
      const list = shotCaptions(getState().project, shot.id);
      const cur = list[i] || null;
      const id = cur ? cur.id : i === list.length ? created.current : null;
      if (id) {
        if (!v.trim()) {
          if (id === created.current) created.current = null;
          await ops([{ op: "caption.delete", id }], { label: "刪除字幕" });
        } else {
          await ops([{ op: "caption.update", id, patch: { text: v } }], { label: "修改字幕", coalesce: `caption-text:${id}` });
        }
        return;
      }
      const seq = buildSequence(getState().project).find((c) => c.shot.id === shot.id);
      if (!v.trim() || !seq || i !== list.length) return;
      const at = nextLine(list, seq);
      if (!at) {
        toast("最後一句太短，分不出下一句，請先到剪輯頁延長字幕或鏡頭");
        return;
      }
      const position = list.length ? list[list.length - 1].position || "bottom" : "bottom";
      const item = { id: newId("c"), start: at.start, end: at.end, text: v, position, shot: shot.id };
      created.current = item.id;
      if (!(await ops([...at.room, { op: "caption.add", item }], { label: "新增字幕" }))) created.current = null;
    });
  };
  const first = caps[0];
  const last = caps[caps.length - 1];
  const hint = first
    ? `${caps.length > 1 ? `${caps.length} 句字幕，` : "字幕"}在 ${fmtSeconds(first.start, 2)} 到 ${fmtSeconds(last.end, 2)}；這裡只同步文字，時間與長度到剪輯頁調整；清空一句就移除那句`
    : clip ? "填寫後會在這個鏡頭的位置新增一句字幕並綁定；清空就移除" : "這個鏡頭不在輸出序列，放入序列後才能加字幕";
  return html`<${Section} title="字幕" icon="captions">
    <${Field} hint=${hint}>
      <div class="caption-lines">
        ${lines.map((cap, i) => html`<${DraftText} key=${i} multiline rows=${2} value=${cap ? cap.text || "" : ""} maxlength="500"
          disabled=${!cap && !clip} onCommit=${commit(i)}
          placeholder=${i === 0 ? "這個鏡頭要說的話（選填）" : "下一句（留空不新增）"} />`)}
      </div>
    <//>
    ${first && html`<div class="row">
      <${Button} size="sm" icon="scissors" onClick=${() => {
        navigate("edit", { editSel: { type: "caption", id: first.id } });
        setTime(first.start);
      }}>在剪輯頁查看<//>
      ${clip && !adding && html`<${Button} size="sm" kind="ghost" icon="plus"
        title="在最後一句之後加一句；鏡頭後段沒有空檔時，把最後一句的後半段分給新的一句" onClick=${() => setAdding(true)}>再加一句<//>`}
      <${Button} size="sm" kind="ghost" icon="link" title="字幕留在時間軸上，只是不再顯示在這個分鏡"
        onClick=${() => ops(caps.map((c) => ({ op: "caption.update", id: c.id, patch: { shot: null } })), { label: "解除綁定鏡頭" })}>
        ${caps.length > 1 ? "全部解除綁定" : "解除綁定"}<//>
    </div>`}
  <//>`;
}

export function ContentTab({ project, shot }) {
  const presets = useStore((s) => s.presets);
  const sizes = presets.shotSizes || NONE;
  const moves = presets.cameraMoves || NONE;
  const save = (field, label) => (v) => updateShot(shot.id, { [field]: v }, { coalesce: `${shot.id}:${field}`, label });
  const [prompt, setPrompt, flushPrompt] = useDraft(shot.prompt || "", save("prompt", "修改鏡頭描述"));
  const [camera, setCamera, flushCamera] = useDraft(shot.camera || "", save("camera", "修改運鏡"));
  const [motion, setMotion, flushMotion] = useDraft(shot.motion || "", save("motion", "修改動作描述"));
  const now = (set, flush) => (v) => {
    set(v);
    flush();
  };
  const putPrompt = now(setPrompt, flushPrompt);
  const putCamera = now(setCamera, flushCamera);
  const putMotion = now(setMotion, flushMotion);

  const lead = leadingSize(prompt, sizes);
  const toggleSize = (s) => {
    if (lead && lead.size.id === s.id) {
      putPrompt(lead.rest);
      return;
    }
    const base = lead ? lead.rest : prompt.trimStart();
    putPrompt(preferChinese(base) ? `${s.text}，${base}` : `${s.en}, ${base}`);
  };
  const moveOn = (m) => camera === m.text || camera === m.en;
  const toggleMove = (m) => putCamera(moveOn(m) ? "" : preferChinese(prompt) ? m.text : m.en);

  const stylePrompt = (project.style && project.style.prompt) || "";
  const named = castInText(project, prompt).map((m) => m.name.trim());
  const promptHint = [
    named.length ? `會帶入人物外觀：${named.join("、")}` : "",
    stylePrompt ? `全片風格會接在後面：${stylePrompt.length > 40 ? `${stylePrompt.slice(0, 40)}…` : stylePrompt}` : "",
  ].filter(Boolean).join("；") || "主體、動作、場景、光線寫得越具體，生成結果越穩定";
  const hasVideo = !!videoTake(project, shot);

  return html`
    <${Section} title="畫面" icon="image">
      <${Field} label="鏡頭名稱">
        <${DraftText} value=${shot.name || ""} placeholder="鏡頭名稱（選填）" maxLength=${120}
          onCommit=${save("name", "重新命名鏡頭")} />
      <//>
      <${Field} label="鏡頭描述" hint=${promptHint}
          after=${html`<span class="char-count mono" title="字數">${prompt.length}</span>
            <${EnhanceButton} text=${prompt} kind="image" onApply=${putPrompt} />`}>
        <${TextArea} value=${prompt} onChange=${setPrompt} onBlur=${flushPrompt} rows=${4} autosize maxLength=${8000}
          placeholder="描述這個鏡頭的畫面：主體、動作、場景、光線…" />
      <//>
      <${Field} label="景別" hint="會寫在描述的最前面，再點一次可以取消">
        <div class="chips">
          ${sizes.map((s) => html`<${Chip} key=${s.id} active=${!!lead && lead.size.id === s.id} title=${s.en}
            onClick=${() => toggleSize(s)}>${s.label}<//>`)}
        </div>
      <//>
    <//>
    <${CaptionSection} project=${project} shot=${shot} />
    <${Section} title="運鏡與動作" icon="camera">
      <${Field} label="運鏡" hint="生影片時會加進提示詞；做運鏡動畫時也會照這裡決定推拉方向">
        <div class="chips">
          ${moves.map((m) => html`<${Chip} key=${m.id} active=${moveOn(m)} title=${m.text}
            onClick=${() => toggleMove(m)}>${m.label}<//>`)}
        </div>
        <${TextInput} value=${camera} onChange=${setCamera} onBlur=${flushCamera} maxLength=${200} class="mt-6"
          placeholder="或自行描述運鏡，例如：鏡頭穿過窗戶推進室內" />
      <//>
      <${Field} label="影片中的動作"
          after=${html`<${EnhanceButton} text=${motion} kind="motion" onApply=${putMotion} />`}>
        <${TextArea} value=${motion} onChange=${setMotion} onBlur=${flushMotion} rows=${2} autosize maxLength=${4000}
          placeholder="影片中的動作，例如：她轉身看向窗外，頭髮被風吹起" />
      <//>
    <//>
    <${Section} title="負向提示詞" icon="x-circle" collapsible id="insp-negative" defaultOpen=${false}>
      <${Field} hint="會和全片負向提示詞合併；兩者都空白時使用預設值">
        <${DraftText} multiline rows=${2} value=${shot.negative || ""} maxLength=${4000}
          placeholder="這個鏡頭不想出現的內容" onCommit=${save("negative", "修改負向提示詞")} />
      <//>
    <//>
    <${Section} title="長度與標記" icon="clock">
      <${Field} label="片長"
          hint=${hasVideo ? "已有影片時，序列中的長度以影片與剪輯點為準" : "生成影片的目標長度；只有圖片時就是序列中的長度"}>
        <${NumberInput} value=${shot.duration} min=${0.2} max=${600} step=${0.5} digits=${2} suffix="秒"
          onCommit=${(duration) => updateShot(shot.id, { duration }, { label: "修改片長" })} />
      <//>
      <${Field} label="標籤顏色">
        <${ColorSwatches} value=${shot.color ?? null}
          onChange=${(color) => updateShot(shot.id, { color }, { label: "修改標籤顏色" })} />
      <//>
      <${Field} label="備註">
        <${DraftText} multiline rows=${2} value=${shot.notes || ""} maxLength=${4000} placeholder="給自己或團隊的提醒…"
          onCommit=${save("notes", "修改鏡頭備註")} />
      <//>
    <//>`;
}
