// Cast editor: the project's characters and the voice, speed and volume each one speaks with, from the
// local Audio8Voice or the VoAI cloud (speaker, style and model). Captions name a speaker from this list;
// every change is a project op, so it travels with the project package and is undoable. Picking an
// Audio8Voice voice prepares it right away (see prepareVoice).
import { html } from "../lib/h.js";
import { getState, useStore } from "../lib/store.js";
import { ops, navigate } from "../lib/actions.js";
import { newId, round } from "../lib/format.js";
import { loadVoices, prepareVoice, prepareJob, isVoai } from "../lib/voice.js";
import { openModal, Modal, confirmDialog, openMenu } from "../components/overlay.js";
import { Button, IconButton, DraftText, NumberInput, Select, Empty, Spinner, Dot } from "../components/ui.js";
import { Icon } from "../components/icons.js";

const NONE = [];
const VOAI_MAX_SPEED = 1.5;
const ENGINES = [{ value: "voai", label: "雲端" }, { value: "audio8voice", label: "本機" }];
const update = (id, patch, label) => ops([{ op: "cast.update", id, patch }], { label });
const openSettings = (close) => { close(); navigate("engines", { enginesTab: "voice" }); };

// A saved value missing from the list (offline, or gone upstream) stays visible as the first option.
function keep(list, value, online) {
  if (!value || list.some((o) => o.value === value)) return list;
  return [{ value, label: `${value}${online ? "（VoAI 找不到）" : ""}` }, ...list];
}

function Status({ voice, close }) {
  if (!voice || (voice.loading && !voice.status)) {
    return html`<div class="cast-status"><${Spinner} /><span class="dim">連線 Audio8Voice…</span></div>`;
  }
  if (!voice.status.online || voice.error) {
    return html`<div class="note note-warn" role="status"><${Icon} name="alert" size=${15} />
      <span class="note-text"><b>Audio8Voice 沒有連線</b>：${voice.error}。人物表仍可編輯，
        要啟動 Audio8Voice 才能挑選本機聲音與生成配音，也可以改用 VoAI 雲端。</span>
      <${Button} size="sm" icon="refresh" busy=${voice.loading} onClick=${loadVoices}>重新連線<//>
      <${Button} size="sm" kind="ghost" onClick=${() => openSettings(close)}>連線設定<//>
    </div>`;
  }
  const { voices, presets, status } = voice;
  return html`<div class="cast-status">
    <${Dot} tone="ok" />
    <span>Audio8Voice 已連線，${voices.length} 個聲音、${presets.length} 個角色預設</span>
    ${!presets.length && html`<span class="cast-tip dim" title="在 Audio8Voice 視窗按「存成預設…」建立角色">
      （在 Audio8Voice 視窗按「存成預設…」建立角色）</span>`}
    <span class="grow"></span>
    <span class="cast-now dim">目前聲音：${status.voice_name || status.voice || "未選擇"}</span>
    <${IconButton} icon="refresh" size=${15} title="重新讀取聲音與角色預設" disabled=${voice.loading} onClick=${loadVoices} />
  </div>`;
}

function VoaiStatus({ voai, loading, close }) {
  const setup = html`<${Button} size="sm" kind="ghost" onClick=${() => openSettings(close)}>金鑰設定<//>`;
  if (!voai) return html`<div class="cast-status"><${Spinner} /><span class="dim">連線 VoAI 雲端…</span></div>`;
  if (!voai.configured) {
    return html`<div class="cast-status"><${Dot} tone="neutral" />
      <span>VoAI 雲端配音還沒設定金鑰，設定後人物可以改用雲端聲音</span><span class="grow"></span>${setup}</div>`;
  }
  if (!voai.online) {
    return html`<div class="note note-warn" role="status"><${Icon} name="alert" size=${15} />
      <span class="note-text"><b>VoAI 雲端無法使用</b>：${voai.error}</span>
      <${Button} size="sm" icon="refresh" busy=${loading} onClick=${() => loadVoices(true)}>重新連線<//>${setup}
    </div>`;
  }
  const counts = voai.models.map((m) => `${m.version} ${m.speakers.length}`).join("、");
  return html`<div class="cast-status">
    <${Dot} tone="ok" /><span>VoAI 雲端已連線，聲音數：${counts}</span><span class="grow"></span>
    <${IconButton} icon="refresh" size=${15} title="重新讀取 VoAI 聲音" disabled=${loading} onClick=${() => loadVoices(true)} />
  </div>`;
}

const speakerLabel = (sp) => {
  const about = [sp.gender, sp.age ? `${sp.age} 歲` : ""].filter(Boolean).join("，");
  return about ? `${sp.name}（${about}）` : sp.name;
};

// VoAI patches. A speaker or model change keeps the style when the new pick still offers it.
function voaiPatch(member, models, { model = member.model, voice = member.voice }) {
  const m = models.find((x) => x.version === model);
  const sp = m && m.speakers.find((x) => x.name === voice);
  if (m && !sp) return { model, voice: "", voiceName: "", style: "" };
  const styles = sp ? sp.styles : [];
  const style = !sp || styles.includes(member.style) ? member.style || "" : styles[0] || "";
  return { model, voice, voiceName: voice, style };
}

function voaiPicker({ member, voai }) {
  const online = !!(voai && voai.online);
  const models = online ? voai.models : NONE;
  const model = models.find((m) => m.version === member.model);
  const sp = model && model.speakers.find((x) => x.name === member.voice);
  const speakers = keep((model ? model.speakers : NONE).map((x) => ({ value: x.name, label: speakerLabel(x) })), member.voice, online);
  const styles = keep((sp ? sp.styles : NONE).map((x) => ({ value: x, label: x })), member.style, online);
  const versions = keep(models.map((m) => ({ value: m.version, label: m.version })), member.model, online);
  const pick = (choice, label) => update(member.id, voaiPatch(member, models, choice), label);
  return {
    speaker: html`<${Select} value=${member.voice || null} options=${speakers} placeholder="選擇聲音" disabled=${!online}
      title=${online ? "VoAI 的聲音" : "VoAI 雲端無法使用"} onChange=${(v) => pick({ voice: v }, "更換配音聲音")} />`,
    extra: html`<div class="cast-cloud">
      <span class="dim">模型</span>
      <${Select} value=${member.model || null} options=${versions} placeholder="選擇模型" disabled=${!online}
        onChange=${(v) => pick({ model: v }, "更換 VoAI 模型")} />
      <span class="dim">風格</span>
      <${Select} value=${member.style || null} options=${styles} placeholder="選擇風格" disabled=${!online || !sp}
        onChange=${(v) => update(member.id, { style: v }, "更換配音風格")} />
    </div>`,
  };
}

function voiceOptions(member, voices, online) {
  const list = voices.map((v) => ({ value: v.id, label: v.name }));
  if (member.voice && !voices.some((v) => v.id === member.voice)) {
    list.unshift({ value: member.voice, label: `${member.voiceName || member.voice}${online ? "（Audio8Voice 找不到）" : ""}` });
  }
  return list;
}

function presetMenu(e, presets, onPick) {
  openMenu(e, [
    { header: "Audio8Voice 角色預設" },
    ...presets.map((p) => ({ label: p.name, hint: `${p.voice_name || p.voice}，${p.speed} 倍`, onClick: () => onPick(p) })),
  ]);
}

function PrepNote({ job }) {
  const running = job.status === "running";
  return html`<span class="cast-prep dim" role="status" title="Audio8Voice 先轉錄這個聲音的參考音，第一句配音就不用在佇列裡等">
    ${running && html`<${Spinner} size=${11} />`}${running ? "準備聲音中…" : "準備聲音排隊中，等顯示卡空出"}</span>`;
}

function MemberRow({ member, lines, voices, presets, online, voai, prep }) {
  const name = member.name || "未命名";
  const remove = async () => {
    if (lines) {
      const ok = await confirmDialog({
        title: `刪除人物「${name}」？`,
        message: `${lines} 句字幕的人物會變成未指定，已生成的配音仍留在音軌上。按 Ctrl+Z 可以復原。`,
        confirmLabel: "刪除人物",
        danger: true,
      });
      if (!ok) return;
    }
    ops([{ op: "cast.delete", id: member.id }], { label: "刪除人物" });
  };
  const pickVoice = (id) => {
    const v = voices.find((x) => x.id === id);
    update(member.id, { voice: id, voiceName: v ? v.name : member.voiceName || "" }, "更換配音聲音");
    prepareVoice(id);
  };
  const applyPreset = (p) => {
    update(member.id, { voice: p.voice, voiceName: p.voice_name || "", speed: p.speed }, "套用角色預設");
    prepareVoice(p.voice);
  };
  const cloud = isVoai(member);
  const pickEngine = (engine) => {
    if (engine === (cloud ? "voai" : "audio8voice")) return;
    const clear = { engine, voice: "", voiceName: "", style: "", model: "" };
    if (engine === "audio8voice") return update(member.id, clear, "改用 Audio8Voice 本機配音");
    const model = ((voai && voai.models) || NONE)[0];
    const speed = Number(member.speed ?? 1) > VOAI_MAX_SPEED ? { speed: VOAI_MAX_SPEED } : {};
    return update(member.id, { ...clear, model: model ? model.version : "", ...speed }, "改用 VoAI 雲端配音");
  };
  const picker = cloud ? voaiPicker({ member, voai }) : null;
  return html`<li class="cast-row">
    <${DraftText} value=${member.name || ""} maxlength="40" placeholder="人物名稱" aria-label="人物名稱"
      onCommit=${(v) => v.trim() && v.trim() !== member.name && update(member.id, { name: v.trim() }, "人物名稱")} />
    <div class="cast-voice">
      <${Select} class="cast-engine" value=${cloud ? "voai" : "audio8voice"} options=${ENGINES}
        title="VoAI 雲端或本機 Audio8Voice" onChange=${pickEngine} />
      ${cloud ? picker.speaker : html`
        <${Select} value=${member.voice || null} options=${voiceOptions(member, voices, online)} placeholder="選擇聲音"
          disabled=${!online} title=${online ? "Audio8Voice 的聲音" : "Audio8Voice 沒有連線"} onChange=${pickVoice} />
        <${IconButton} icon="sparkles" size=${15} title=${presets.length ? "套用角色預設（聲音與語速）" : "Audio8Voice 還沒有角色預設"}
          disabled=${!presets.length} onClick=${(e) => presetMenu(e, presets, applyPreset)} />`}
    </div>
    <${NumberInput} value=${Number(member.speed ?? 1)} min=${0.5} max=${cloud ? VOAI_MAX_SPEED : 2} step=${0.05} digits=${2}
      suffix="倍" title=${cloud ? "語速（VoAI 0.5 到 1.5 倍）" : "語速"}
      onCommit=${(v) => update(member.id, { speed: round(v, 2) }, "人物語速")} />
    <${NumberInput} value=${Math.round(Number(member.volume ?? 1) * 100)} min=${0} max=${400} step=${5} digits=${0} suffix="%"
      title="配音放到音軌時的音量" onCommit=${(v) => update(member.id, { volume: round(v / 100, 2) }, "人物音量")} />
    <span class="cast-lines dim">${lines} 句</span>
    <${IconButton} icon="trash" size=${15} title="刪除人物" onClick=${remove} />
    ${cloud && picker.extra}
    ${prep && html`<${PrepNote} job=${prep} />`}
  </li>`;
}

function CastBody({ close }) {
  const { project, voice, jobs } = useStore((s) => ({ project: s.project, voice: s.voice, jobs: s.jobs }));
  if (!project) return null;
  const cast = project.cast || NONE;
  const online = !!(voice && voice.status && voice.status.online && !voice.error);
  const voices = online ? voice.voices : NONE;
  const presets = online ? voice.presets : NONE;
  const lines = (id) => (project.captions || []).filter((c) => c.speaker === id).length;

  const add = (member) => {
    ops([{ op: "cast.add", item: { id: newId("p"), volume: 1, speed: 1, ...member } }], { label: "新增人物" });
    if (member.voice) prepareVoice(member.voice);
  };
  const addBlank = () => {
    const st = online ? voice.status : {};
    add({ name: `人物 ${cast.length + 1}`, voice: st.voice || "", voiceName: st.voice_name || "" });
  };
  const addPreset = (e) => presetMenu(e, presets, (p) => add({ name: p.name, voice: p.voice, voiceName: p.voice_name || "", speed: p.speed }));

  const footer = html`<span class="modal-foot-note dim"><${Icon} name="info" size=${14} />在字幕屬性替每句字幕選擇人物</span>
    <${Button} icon="sparkles" disabled=${!presets.length} onClick=${addPreset}
      title=${presets.length ? "" : "Audio8Voice 還沒有角色預設"}>從角色預設新增<//>
    <${Button} icon="plus" onClick=${addBlank}>新增人物<//>
    <${Button} kind="primary" onClick=${() => close(null)}>完成<//>`;
  return html`<${Modal} title="人物表" width=${760} footer=${footer} backdropClose=${false} onClose=${() => close(null)} class="cast-dialog">
    <${VoaiStatus} voai=${voice && voice.voai} loading=${!!(voice && voice.loading)} close=${() => close(null)} />
    <${Status} voice=${voice} close=${() => close(null)} />
    ${cast.length
      ? html`<div class="cast-head dim"><span>人物</span><span>聲音</span><span>語速</span><span>音量</span><span></span><span></span></div>
        <ul class="cast-list">
          ${cast.map((m) => html`<${MemberRow} key=${m.id} member=${m} lines=${lines(m.id)} voices=${voices}
            presets=${presets} online=${online} voai=${voice && voice.voai} prep=${isVoai(m) ? null : prepareJob(jobs, m.voice)} />`)}
        </ul>`
      : html`<${Empty} icon="users" title="還沒有人物">
          新增旁白或角色，替每個人物挑一個聲音（VoAI 雲端或本機 Audio8Voice），再到字幕屬性指定誰說這句話。<//>`}
  <//>`;
}

export function openCastDialog() {
  if (!getState().project) return Promise.resolve(null);
  loadVoices();
  return openModal((close) => html`<${CastBody} close=${close} />`);
}
