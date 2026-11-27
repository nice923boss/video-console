// The 壓縮器 part of an audio clip's properties and of a shot's own sound (voice-panel.js VoicePanel, so op is
// "audio.update" or "shot.update"): the clip's `comp` settings after the Logic Compressor in Final Cut Pro. The
// preview runs lib/compressor.js in lib/compressor-worklet.js, the export renders the same (vc/compressor.py).
import { html, useState, useEffect } from "../lib/h.js";
import { ops } from "../lib/actions.js";
import { round } from "../lib/format.js";
import { DEFAULT_COMP } from "../lib/compressor.js";
import { readReduction } from "../lib/audio-mix.js";
import { Field, Slider, Segmented, Toggle, Section, Select, NumberInput, Button } from "./ui.js";

const CIRCUITS = ["Platinum Digital", "Studio VCA", "Studio FET", "Classic VCA", "Vintage VCA", "Vintage FET", "Vintage Opto"];
// Settings of the presets that come with Final Cut Pro's compressor (name, threshold, ratio, attack, release,
// makeup, knee, circuit); everything else stays as the clip has it.
const PRESETS = [
  ["口白（強）", -22, 6, 2, 34, 0, 1, 0],
  ["口白（自然）", -14.5, 1.6, 18.5, 700, -8.5, 1, 0],
  ["口白（輕快）", -14.5, 2.8, 16.5, 29, -0.5, 1, 0],
  ["柔和人聲", -9.5, 2.4, 19, 310, 0, 1, 0],
  ["饒舌人聲", -20.5, 8, 9.5, 98, 0, 0.4, 0],
  ["混音加強 1", -14.5, 3, 26, 29, 0, 1, 0],
  ["混音加強 2（背景音樂）", -14.5, 3, 29, 1300, 0, 1, 0],
  ["光學慢釋放", -7, 1.9, 44, 1400, 1, 1, 6],
  ["光學緊壓", -13.5, 6.6, 5.5, 160, 4, 0.7, 6],
  ["重壓 VCA", -40, 5.6, 5, 51, 19, 0, 4],
].map(([name, threshold, ratio, attack, release, makeup, knee, circuit]) =>
  ({ name, patch: { threshold, ratio, attack, release, makeup, knee, circuit } }));
const AUTO_GAIN = [{ value: 0, label: "關" }, { value: 1, label: "0 dB" }, { value: 2, label: "−12 dB" }];
const DISTORTION = [
  { value: "off", label: "關" }, { value: "soft", label: "柔和" }, { value: "hard", label: "硬" }, { value: "clip", label: "削波" },
];
const sign = (v) => (v < 0 ? "−" : v > 0 ? "+" : "");
const fmtDb = (v) => `${sign(round(v, 1))}${Math.abs(round(v, 1))} dB`;
const fmtVal = (v, digits, unit) => (unit === "dB" ? fmtDb(v) : `${round(v, digits)}${unit ? ` ${unit}` : ""}`);

// Slider position (0 to 1) of a value on a scale that gives the low end more room: ratio, attack, release.
const toU = (v, lo, hi) => Math.log(v - lo + 1) / Math.log(hi - lo + 1);
const fromU = (u, lo, hi) => lo + (hi - lo + 1) ** u - 1;

// One setting: a slider and the exact number beside it; a number out of range is clamped when it is entered.
function Param({ label, value, min, max, step, digits, unit, curved = false, disabled, onCommit }) {
  const commit = (v) => onCommit(round(Math.min(max, Math.max(min, v)), digits));
  return html`<${Field} label=${label} hint=${`範圍 ${fmtVal(min, digits, unit)} 到 ${fmtVal(max, digits, unit)}，超出會自動調回範圍內`}
    after=${html`<${NumberInput} value=${value} min=${min} max=${max} step=${step} digits=${digits} disabled=${disabled}
      onCommit=${commit} class="comp-num" title="輸入精確數值" />`}>
    ${curved
      ? html`<${Slider} value=${toU(value, min, max)} min=${0} max=${1} step=${0.001} disabled=${disabled}
          format=${(u) => fmtVal(fromU(u, min, max), digits, unit)} onCommit=${(u) => commit(fromU(u, min, max))} />`
      : html`<${Slider} value=${value} min=${min} max=${max} step=${step} disabled=${disabled}
          format=${(v) => fmtVal(v, digits, unit)} onCommit=${commit} />`}
  <//>`;
}

// How much the compressor turns the clip down right now, while it plays.
function ReductionMeter({ id }) {
  const [gr, setGr] = useState(null);
  useEffect(() => {
    let raf = 0;
    let last = null;
    const tick = () => {
      const v = readReduction(id);
      const shown = v == null ? null : round(v, 1);
      if (shown !== last) setGr((last = shown));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [id]);
  const pct = gr == null ? 0 : Math.min(100, (-gr / 30) * 100);
  return html`<${Field} label="增益衰減（Gain Reduction）" hint="播放時顯示壓縮器目前把聲音壓低多少，滿格為 30 dB">
    <div class="comp-meter">
      <div class="comp-meter-track"><div class="comp-meter-fill" style=${`width:${pct}%`}></div></div>
      <span class="comp-meter-val mono">${gr == null ? "未播放" : fmtDb(gr)}</span>
    </div>
  <//>`;
}

export function CompSection({ item, op }) {
  const [more, setMore] = useState(false);
  const cp = { ...DEFAULT_COMP, ...(item.comp || {}), on: !!(item.comp && item.comp.on) };
  const set = (patch, label) => ops([{ op, id: item.id, patch: { comp: { ...cp, ...patch } } }], { label: `壓縮器：${label}` });
  const off = !cp.on;
  const param = (k, label, min, max, step, digits, unit, curved = false) => html`<${Param} key=${k} label=${label}
    value=${cp[k]} min=${min} max=${max} step=${step} digits=${digits} unit=${unit} curved=${curved} disabled=${off}
    onCommit=${(v) => set({ [k]: v }, label.replace(/（.*）/, ""))} />`;
  const platinum = cp.circuit === 0;
  return html`<${Section} title="壓縮器" icon="gauge" collapsible id="voice-comp">
    <${Toggle} checked=${cp.on} label="開啟壓縮器" hint="把大聲的地方壓小，讓音量更平均"
      onChange=${(on) => set({ on }, on ? "開啟" : "關閉")} />
    <${Field} label="預設組">
      <${Select} value=${null} placeholder="選擇預設組套用（會同時開啟壓縮器）"
        options=${PRESETS.map((p) => ({ value: p.name, label: p.name }))}
        onChange=${(name) => set({ ...PRESETS.find((p) => p.name === name).patch, on: true }, `套用預設組 ${name}`)} />
    <//>
    <${Field} label="電路類型（Circuit Type）" hint="不同電路的反應快慢與聲音色彩不同；Platinum Digital 最乾淨">
      <${Select} value=${cp.circuit} disabled=${off} options=${CIRCUITS.map((label, value) => ({ value, label }))}
        onChange=${(circuit) => set({ circuit }, `電路類型 ${CIRCUITS[circuit]}`)} />
    <//>
    <${ReductionMeter} id=${item.id} />
    ${param("threshold", "臨界值（Threshold）", -60, 0, 0.5, 1, "dB")}
    ${param("ratio", "壓縮比（Ratio）", 1, 30, 0.1, 1, ": 1", true)}
    ${param("knee", "膝部（Knee）", 0, 1, 0.1, 1, "")}
    ${param("attack", "起音（Attack）", 0, 200, 0.5, 1, "ms", true)}
    ${param("release", "釋放（Release）", 5, 5000, 1, 0, "ms", true)}
    <${Toggle} checked=${cp.autoRelease} disabled=${off} label="自動釋放（Auto）" hint="持續壓縮時自動放慢釋放，減少音量忽大忽小"
      onChange=${(autoRelease) => set({ autoRelease }, autoRelease ? "開啟自動釋放" : "關閉自動釋放")} />
    ${param("makeup", "補償增益（Make Up）", -30, 30, 0.5, 1, "dB")}
    <${Field} label="自動增益（Auto Gain）" hint="壓縮後自動補回音量：0 dB 讓滿格的聲音維持滿格，−12 dB 以 −12 dBFS 為準">
      <${Segmented} size="sm" value=${cp.autoGain} options=${AUTO_GAIN} disabled=${off}
        onChange=${(autoGain) => set({ autoGain }, `自動增益 ${AUTO_GAIN[autoGain].label}`)} />
    <//>
    <div class="row">
      <${Button} size="sm" kind="ghost" icon=${more ? "chevron-up" : "chevron-down"} onClick=${() => setMore(!more)}>
        ${more ? "收起進階設定" : "進階設定"}<//>
    </div>
    ${more && html`
      <${Field} label="偵測方式（Peak／RMS）" hint=${platinum ? "Peak 反應快，RMS 接近耳朵感受的音量" : "只有 Platinum Digital 可以選，其他電路固定"}>
        <${Segmented} size="sm" value=${cp.rms ? "rms" : "peak"} disabled=${off || !platinum}
          options=${[{ value: "peak", label: "Peak" }, { value: "rms", label: "RMS" }]}
          onChange=${(m) => set({ rms: m === "rms" }, `偵測方式 ${m === "rms" ? "RMS" : "Peak"}`)} />
      <//>
      <${Field} label="左右聲道偵測（Side Chain）" hint="Max：取左右較大聲的一邊；Sum：左右合起來算">
        <${Segmented} size="sm" value=${cp.link} disabled=${off}
          options=${[{ value: "max", label: "Max" }, { value: "sum", label: "Sum" }]}
          onChange=${(link) => set({ link }, `左右聲道偵測 ${link === "sum" ? "Sum" : "Max"}`)} />
      <//>
      <${Toggle} checked=${cp.lookahead} disabled=${off} label="預讀（Lookahead）" hint="提早 5 毫秒看到大聲的地方，壓得更及時"
        onChange=${(lookahead) => set({ lookahead }, lookahead ? "開啟預讀" : "關閉預讀")} />
      <${Toggle} checked=${cp.limiter} disabled=${off} label="限幅器（Limiter）" hint="最後一道關卡，聲音不超過限幅臨界值"
        onChange=${(limiter) => set({ limiter }, limiter ? "開啟限幅器" : "關閉限幅器")} />
      ${cp.limiter && param("limiterThreshold", "限幅臨界值（Limiter Threshold）", -20, 0, 0.5, 1, "dB")}
      <${Field} label="失真（Distortion）" hint="刻意加上的失真色彩；柔和最溫和，削波最硬">
        <${Segmented} size="sm" value=${cp.distortion} options=${DISTORTION} disabled=${off}
          onChange=${(distortion) => set({ distortion }, `失真 ${DISTORTION.find((d) => d.value === distortion).label}`)} />
      <//>
      ${param("mix", "乾濕比（Mix）", 0, 1, 0.01, 2, "")}
      ${param("inGain", "輸入增益（Input Gain）", -30, 30, 0.5, 1, "dB")}
      ${param("outGain", "輸出增益（Output Gain）", -30, 30, 0.5, 1, "dB")}
      <div class="insp-hint">乾濕比 1 是只聽壓縮後的聲音，0 是只聽原音；調整任何設定時，預覽的壓縮器會從頭開始反應。</div>`}
  <//>`;
}
