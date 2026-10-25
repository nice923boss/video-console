// The 聲道 / 降噪 / 去回音 part of an audio clip's properties (edit-props.js AudioPanel): which channel of its file
// the clip plays, noise reduction learned from a stretch of noise in the clip, and the classic echo remover. The
// preview plays them from stems (lib/voice-stems.js) and the export renders the same (vc/voiceclean.py).
import { html, useState, useEffect } from "../lib/h.js";
import { getState, setState, useStore } from "../lib/store.js";
import { ops } from "../lib/actions.js";
import { api } from "../lib/api.js";
import { clock } from "../lib/clock.js";
import { clamp, fmtSeconds, round } from "../lib/format.js";
import { audioLength, audioSpeed } from "../lib/sequence.js";
import { DEFAULT_DENOISE, DEFAULT_DEREVERB, MAX_SAMPLE, passParams } from "../lib/voice-mix.js";
import { Field, Slider, Segmented, Toggle, Section, Button, Spinner } from "./ui.js";
import { Icon } from "./icons.js";
import { toast } from "./toast.js";

const SAMPLE_LEN = 1; // seconds of noise taken at the playhead when no I/O range is marked on the clip
const MODES = [
  { value: "fx", label: "處理後" },
  { value: "dry", label: "原音" },
  { value: "delta", label: "只聽被移除的" },
];
const pct = (v) => `${Math.round(v * 100)}%`;
const db = (v) => `${v > 0 ? "+" : ""}${round(v, 1)} dB`;
const update = (id, patch, label) => ops([{ op: "audio.update", id, patch }], { label });
const levels = new Map(); // what voice-channels gave, by clip and span: the overview decodes the whole span

// The stretch of the clip's source file (seconds) taken as noise: the I/O range marked on the timeline where it
// lies on the clip, else SAMPLE_LEN seconds from the playhead. Null when neither is on the clip.
function noiseSample(item, media, range) {
  const start = Number(item.start || 0);
  const len = audioLength(item, media);
  const tIn = Number(item.in || 0);
  const src = (t) => tIn + clamp(t - start, 0, len) * audioSpeed(item);
  const end = src(start + len);
  if (range && range.out > range.in && range.in < start + len && range.out > start) {
    return { from: round(src(range.in), 3), to: round(Math.min(src(range.out), src(range.in) + MAX_SAMPLE), 3) };
  }
  if (clock.time < start || clock.time >= start + len) return null;
  const from = Math.max(tIn, Math.min(src(clock.time), end - SAMPLE_LEN));
  return { from: round(from, 3), to: round(Math.min(end, from + SAMPLE_LEN), 3) };
}

function channelLabel(streams, stream, index) {
  if (streams.length > 1) return `音軌 ${stream.stream + 1}・聲道 ${index + 1}`;
  if (stream.channels.length === 2) return `聲道 ${index + 1}（${index ? "右" : "左"}）`;
  return `聲道 ${index + 1}`;
}

function Bars({ peaks }) {
  const d = peaks.map((v, i) => `M${i + 0.5} 24V${round(24 - (v * 24) / 100, 1)}`).join("");
  return html`<svg class="vch-bars" viewBox=${`0 0 ${peaks.length} 24`} preserveAspectRatio="none" aria-hidden="true">
    <path d=${d} /></svg>`;
}

function ChannelRow({ label, on, onPick, children }) {
  return html`<button type="button" role="radio" aria-checked=${on ? "true" : "false"} class=${`vch-row${on ? " on" : ""}`}
    onClick=${onPick}>
    <span class="vch-dot"></span><span class="vch-label">${label}</span>${children}
  </button>`;
}

// Every channel of the clip's file with its level over the clip; picking one plays only that channel.
function ChannelSection({ pid, item, media }) {
  const key = JSON.stringify([pid, item.id, media.file, item.in ?? 0, item.out ?? null]);
  const [got, setGot] = useState({ key: "", streams: null, error: "" });
  useEffect(() => {
    if (levels.has(key)) return setGot({ key, streams: levels.get(key), error: "" });
    let live = true;
    api.post(`/api/projects/${encodeURIComponent(pid)}/voice-channels`, { id: item.id })
      .then((res) => {
        levels.set(key, res.streams);
        if (live) setGot({ key, streams: res.streams, error: "" });
      })
      .catch((err) => {
        if (live && err.status !== 499) setGot({ key, streams: null, error: err.message || String(err) });
      });
    return () => {
      live = false;
    };
  }, [key]);
  const streams = got.key === key ? got.streams : null;
  const error = got.key === key ? got.error : "";
  const pick = item.channel;
  const total = streams ? streams.reduce((n, s) => n + s.channels.length, 0) : 0;
  const set = (channel, label) => update(item.id, { channel }, label);

  let body;
  if (error) body = html`<div class="insp-hint warn-text"><${Icon} name="alert" size=${14} />無法讀取聲道音量：${error}</div>`;
  else if (!streams) body = html`<div class="insp-hint"><${Spinner} size=${14} />正在讀取各聲道的音量…</div>`;
  else if (total <= 1 && !pick) body = html`<div class="insp-hint">這個檔案只有一個聲道。</div>`;
  else {
    body = html`<div class="vch-list" role="radiogroup" aria-label="播放的聲道">
      <${ChannelRow} label=${streams.length > 1 ? "第一條音軌（混成立體聲）" : "全部聲道（混成立體聲）"} on=${!pick}
        onPick=${() => pick && set(null, "播放全部聲道")} />
      ${streams.map((s) => s.channels.map((c, index) => {
        const label = channelLabel(streams, s, index);
        const on = !!pick && pick.stream === s.stream && pick.index === index;
        return html`<${ChannelRow} key=${`${s.stream}-${index}`} label=${label} on=${on}
          onPick=${() => !on && set({ stream: s.stream, index }, `只播放${label}`)}>
          <${Bars} peaks=${c.peaks} />
          <span class="vch-db mono">${c.peakDb == null ? "無聲" : `峰值 ${c.peakDb}／平均 ${c.rmsDb} dB`}</span>
        <//>`;
      }))}
    </div>`;
  }
  return html`<${Section} title="聲道" icon="rows" collapsible id="voice-channel">
    ${body}
    <div class="insp-hint">只選一個聲道時，其他聲道不播放也不匯出，降噪與去回音只處理這個聲道；選的聲道放在左右正中央。</div>
  <//>`;
}

function DenoiseSection({ item, media }) {
  const dn = { ...DEFAULT_DENOISE, ...(item.denoise || {}), on: !!(item.denoise && item.denoise.on) };
  const set = (patch, label) => update(item.id, { denoise: { ...dn, ...patch } }, label);
  const take = () => {
    const sample = noiseSample(item, media, getState().editRange);
    if (!sample) return toast("先把播放頭移到這段音訊上只有雜音的地方，或用 I、O 標出一段雜音", { level: "warn" });
    set({ on: true, sample }, "取樣雜訊");
  };
  const s = dn.sample;
  const off = !dn.on || !s;
  return html`<${Section} title="降噪" icon="wand" collapsible id="voice-denoise">
    <${Toggle} checked=${dn.on} label="開啟降噪" hint=${s ? "" : "先取樣雜訊才會有作用"}
      onChange=${(on) => set({ on }, on ? "開啟降噪" : "關閉降噪")} />
    <div class="row">
      <${Button} size="sm" icon="scan" onClick=${take}
        title="取播放頭起 1 秒當雜訊樣本；時間軸上有 I、O 標記且落在這段音訊上時，改用標記的範圍">${s ? "重新取樣雜訊" : "取樣雜訊"}<//>
      ${s && html`<span class="dim mono">檔案內 ${fmtSeconds(s.from, 2)}～${fmtSeconds(s.to, 2)}</span>`}
    </div>
    <div class="insp-hint">把播放頭停在只有雜音、沒有人聲的地方再按；也可以先用 I、O 在時間軸標出一段雜音（0.25 到 10 秒）。</div>
    <${Field} label="強度（Strength）" hint="最多壓低雜音 30 dB">
      <${Slider} value=${dn.strength} min=${0} max=${1} step=${0.01} format=${pct} disabled=${off}
        onCommit=${(v) => set({ strength: round(v, 2) }, "降噪強度")} />
    <//>
    ${[["low", "低頻（Low，250 Hz 以下）"], ["mid", "中頻（Mid）"], ["high", "高頻（High，4 kHz 以上）"]].map(([k, label]) => html`
      <${Field} key=${k} label=${label}>
        <${Slider} value=${dn[k]} min=${0} max=${1} step=${0.01} format=${pct} disabled=${off}
          onCommit=${(v) => set({ [k]: round(v, 2) }, `降噪${label.slice(0, 2)}`)} />
      <//>`)}
    <div class="insp-hint">三個頻段各套用多少強度：100% 照強度降噪，0% 這個頻段不降噪。人聲變悶就調低高頻。</div>
  <//>`;
}

function EchoSection({ item }) {
  const dr = { ...DEFAULT_DEREVERB, ...(item.dereverb || {}), on: !!(item.dereverb && item.dereverb.on) };
  const set = (patch, label) => update(item.id, { dereverb: { ...dr, ...patch } }, label);
  const off = !dr.on;
  const slider = (k, label, format, min = 0) => html`<${Field} key=${k} label=${label}>
    <${Slider} value=${dr[k]} min=${min} max=${1} step=${0.01} format=${format} disabled=${off}
      onCommit=${(v) => set({ [k]: round(v, 2) }, `去回音 ${label}`)} />
  <//>`;
  return html`<${Section} title="去回音" icon="activity" collapsible id="voice-dereverb">
    <${Toggle} checked=${dr.on} label="開啟去回音" hint="不需要取樣，從聲音本身估計房間的殘響"
      onChange=${(on) => set({ on }, on ? "開啟去回音" : "關閉去回音")} />
    ${slider("strength", "強度（Strength）", pct)}
    ${slider("dryness", "乾度（Dryness）", pct)}
    ${slider("release", "釋放（Release）", (v) => `${Math.round(10 * 40 ** v)} ms`)}
    <div class="insp-hint">乾度越高，假設房間越空、去掉越多；釋放越長，聲音越平順但留下較多尾巴，太短會斷斷續續。改這兩個要重新處理這段音訊，其他滑桿立即生效。</div>
    ${slider("bass", "低頻衰減（Bass）", (v) => db(-24 * v))}
    ${slider("body", "厚度（Body）", (v) => db(6 * v), -1)}
    ${slider("tone", "明亮（Tone）", (v) => db(6 * v), -1)}
  <//>`;
}

export function VoicePanel({ project, item, media }) {
  const entry = useStore((s) => (s.voiceStems || {})[item.id]);
  const mode = useStore((s) => (s.voiceMonitor && s.voiceMonitor.id === item.id ? s.voiceMonitor.mode : "fx"));
  useEffect(() => () => setState((s) => (s.voiceMonitor && s.voiceMonitor.id === item.id ? { voiceMonitor: null } : null)),
    [item.id]);
  const active = !!passParams(item);
  return html`
    <${ChannelSection} pid=${project.id} item=${item} media=${media} />
    <${DenoiseSection} item=${item} media=${media} />
    <${EchoSection} item=${item} />
    ${active && html`<${Field} label="預覽試聽" hint="原音是處理前的聲音，沒有補償音量差；只聽被移除的，用來檢查有沒有削到人聲">
      <${Segmented} size="sm" value=${mode} options=${MODES} onChange=${(m) => setState({ voiceMonitor: { id: item.id, mode: m } })} />
    <//>`}
    ${active && entry && entry.busy && html`<div class="insp-hint"><${Spinner} size=${14} />正在處理降噪／去回音，完成前預覽播放原本的聲音。</div>`}
    ${active && entry && !entry.busy && entry.error && html`<div class="insp-hint warn-text">
      <${Icon} name="alert" size=${14} />處理失敗：${entry.error}</div>`}
  `;
}
