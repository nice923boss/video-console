// Level meters for the preview mix, drawn like Final Cut Pro's: peak bars on its scale (+6 dB down to -∞),
// green below -6 dB, yellow up to 0 dB and red above, a peak-hold line, and the held peak in numbers on
// top (click it to reset). Below them: short-term and integrated loudness with the true peak (MX-01) and the
// stereo correlation (MX-09). The mini pair sits in the player bar and opens the full panel, whose left edge
// drags to make it wider or narrower. TrackMeter is the thin bar in a timeline row's header (MX-02).
// One animation loop feeds every mounted meter; bars are written as CSS variables, not re-rendered.
import { html, useEffect, useRef } from "../lib/h.js";
import { readMix, readTrackPeak, readLoudness, resetLoudness } from "../lib/audio-mix.js";
import { clock } from "../lib/clock.js";
import { getState, setState, useStore } from "../lib/store.js";
import { usePref } from "../lib/prefs.js";
import { clamp } from "../lib/format.js";

// [dB, height share] from Final Cut Pro's meter: the scale is stretched around 0 dB and squeezed below -20.
const SCALE = [[-60, 0], [-50, 0.073], [-30, 0.208], [-20, 0.342], [-12, 0.496], [-6, 0.654], [0, 0.85], [6, 1]];
const TICKS = [6, 0, -6, -12, -20, -30, -50];
const FLOOR = -60;
const FALL_DB_S = 26;
const HOLD_MS = 1500;
const CLIP_GAP = 0.5; // seconds: an over this close to a marked one is the same spot
const CORR_S = 0.25; // seconds the correlation reading takes to settle
const TP_LIMIT = -1; // dBTP: the ceiling YouTube, Spotify and the EBU ask for
const W_DEFAULT = 88;
const W_MIN = 72;
const W_MAX = 240;
const validWidth = (v) => Number.isFinite(v) && v >= W_MIN && v <= W_MAX;

function dbToShare(db) {
  if (!(db > FLOOR)) return 0;
  for (let i = 1; i < SCALE.length; i += 1) {
    const [d1, s1] = SCALE[i];
    if (db <= d1) {
      const [d0, s0] = SCALE[i - 1];
      return s0 + ((db - d0) / (d1 - d0)) * (s1 - s0);
    }
  }
  return 1;
}

const toDb = (peak) => (peak > 0 ? 20 * Math.log10(peak) : -Infinity);
const holdLabel = (db) => (db > FLOOR ? String(Math.round(db)) : "-∞");
const loudLabel = (v) => (v > -70 ? v.toFixed(1) : "-∞");

// Marks the playhead time when the mix goes over 0 dB while playing; the timeline ruler shows the marks
// as red dots. They are kept for this session and the open project only.
function markClip(peaks) {
  if (!clock.playing || Math.max(peaks[0], peaks[1]) <= 1) return;
  const { pid, clipMarks } = getState();
  const times = clipMarks && clipMarks.pid === pid ? clipMarks.times : [];
  const t = clock.time;
  if (times.some((m) => Math.abs(m - t) < CLIP_GAP)) return;
  setState({ clipMarks: { pid, times: [...times, t].sort((a, b) => a - b) } });
}

// Shared meter state: bar level and held peak per channel, in dB; the settled correlation (null: silent);
// the frame's time step for meters that fall on their own.
const state = { bars: [FLOOR, FLOOR], holds: [FLOOR, FLOOR], heldAt: [0, 0], corr: null, dt: 0 };
const subscribers = new Set();
let raf = 0;
let lastNow = 0;

function tick(now) {
  raf = subscribers.size ? requestAnimationFrame(tick) : 0;
  const dt = lastNow ? Math.min(0.1, (now - lastNow) / 1000) : 0;
  lastNow = now;
  state.dt = dt;
  const mix = readMix();
  const peaks = mix ? mix.peaks : [0, 0];
  markClip(peaks);
  const corr = mix ? mix.corr : null;
  if (corr === null) state.corr = null;
  else state.corr = state.corr === null ? corr : state.corr + (corr - state.corr) * Math.min(1, dt / CORR_S);
  for (let ch = 0; ch < 2; ch += 1) {
    const db = Math.max(FLOOR, toDb(peaks[ch]));
    state.bars[ch] = Math.max(db, state.bars[ch] - FALL_DB_S * dt);
    if (db >= state.holds[ch]) {
      state.holds[ch] = db;
      state.heldAt[ch] = now;
    } else if (now - state.heldAt[ch] > HOLD_MS) {
      state.holds[ch] = Math.max(state.bars[ch], state.holds[ch] - FALL_DB_S * dt);
    }
  }
  subscribers.forEach((fn) => fn(state));
}

function useMeter(draw) {
  useEffect(() => {
    subscribers.add(draw);
    if (!raf) {
      lastNow = 0;
      raf = requestAnimationFrame(tick);
    }
    return () => {
      subscribers.delete(draw);
      if (!subscribers.size && raf) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
    };
  }, []);
}

function resetHolds() {
  state.holds = [...state.bars];
  state.heldAt = [0, 0];
}

function setBar(node, bar, hold) {
  const st = node.style;
  const lv = dbToShare(bar).toFixed(4);
  const pk = dbToShare(hold).toFixed(4);
  if (st.getPropertyValue("--lv") !== lv) st.setProperty("--lv", lv);
  if (st.getPropertyValue("--pk") !== pk) st.setProperty("--pk", pk);
}

function setText(node, text) {
  if (node && node.textContent !== text) node.textContent = text;
}

// The full panel beside the timeline.
export function AudioMeters() {
  const [width, setWidth] = usePref("vc.edit.metersW", W_DEFAULT, validWidth);
  const barRefs = [useRef(null), useRef(null)];
  const numRefs = [useRef(null), useRef(null)];
  const loudRefs = { short: useRef(null), integrated: useRef(null), peak: useRef(null) };
  const corrRef = useRef(null);
  const pid = useStore((s) => s.pid);
  useEffect(() => resetLoudness(), [pid]); // a measurement covers one project
  useMeter((s) => {
    const loud = readLoudness();
    setText(loudRefs.short.current, loud ? loudLabel(loud.shortTerm) : "-∞");
    setText(loudRefs.integrated.current, loud ? loudLabel(loud.integrated) : "-∞");
    setText(loudRefs.peak.current, loud && loud.truePeak > FLOOR ? loud.truePeak.toFixed(1) : "-∞");
    if (loudRefs.peak.current) loudRefs.peak.current.classList.toggle("is-over", !!loud && loud.truePeak > TP_LIMIT);
    const corr = corrRef.current;
    if (corr) {
      const at = s.corr === null ? "" : ((s.corr + 1) / 2).toFixed(3);
      if (corr.style.getPropertyValue("--at") !== at) corr.style.setProperty("--at", at);
      corr.classList.toggle("is-idle", s.corr === null);
      corr.classList.toggle("is-neg", s.corr !== null && s.corr < 0);
    }
    for (let ch = 0; ch < 2; ch += 1) {
      if (barRefs[ch].current) setBar(barRefs[ch].current, s.bars[ch], s.holds[ch]);
      const num = numRefs[ch].current;
      if (!num) continue;
      const text = holdLabel(s.holds[ch]);
      if (num.textContent !== text) num.textContent = text;
      num.classList.toggle("is-over", s.holds[ch] > 0);
    }
  });
  // The width is written to the panel while dragging and saved once on release.
  const startDrag = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const handle = e.currentTarget;
    const panel = handle.parentElement;
    const startX = e.clientX;
    let w = width;
    handle.setPointerCapture(e.pointerId);
    const move = (ev) => {
      w = Math.round(clamp(width - (ev.clientX - startX), W_MIN, W_MAX));
      panel.style.width = `${w}px`;
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      setWidth(w);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };
  return html`<aside class="meters" aria-label="音量表" style=${`width:${width}px`}>
    <div class="meters-edge" role="separator" aria-orientation="vertical" aria-label="調整音量表寬度"
      title="拖曳調整音量表寬度，雙擊還原" onPointerDown=${startDrag} onDblClick=${() => setWidth(W_DEFAULT)}></div>
    <div class="meters-peaks">
      ${[0, 1].map((ch) => html`<button type="button" class="meters-peak mono" ref=${numRefs[ch]}
        title="峰值，點一下重設" onClick=${resetHolds}>-∞</button>`)}
    </div>
    <div class="meters-body">
      <div class="meters-scale mono" aria-hidden="true">
        ${TICKS.map((db) => html`<span style=${`--at:${dbToShare(db)}`}>${db > 0 ? `+${db}` : db}</span>`)}
        <span style="--at:0">-∞</span>
      </div>
      ${[0, 1].map((ch) => html`<div class="meters-bar" ref=${barRefs[ch]}><b class="meters-hold"></b></div>`)}
    </div>
    <div class="meters-foot mono"><span>L</span><span>R</span></div>
    <div class="meters-corr" ref=${corrRef} role="img" aria-label="立體聲相關度"
      title="立體聲相關度：靠右（+1）左右相同，中間無關，偏左（負值）左右相位相反，手機單喇叭播放時會互相抵消變小聲">
      <span class="mono">-1</span><i><b></b></i><span class="mono">+1</span>
    </div>
    <button type="button" class="meters-loud" onClick=${resetLoudness}
      title="響度依 EBU R128 量測，從上次重設後播放過的部分算起；點一下重新量測。YouTube 約 -14 LUFS，Podcast 約 -16 LUFS，真峰值建議不超過 -1 dBTP">
      <span>短期</span><b class="mono" ref=${loudRefs.short}>-∞</b>
      <span>整合</span><b class="mono" ref=${loudRefs.integrated}>-∞</b>
      <span>真峰值</span><b class="mono" ref=${loudRefs.peak}>-∞</b>
    </button>
  </aside>`;
}

// A timeline row's level as a thin bar along the bottom of its header; it turns red once the row goes over
// 0 dB and stays red until clicked, so the row that clips can be found after playback stops.
export function TrackMeter({ track }) {
  const ref = useRef(null);
  const level = useRef({ bar: FLOOR, over: false });
  useMeter((s) => {
    const node = ref.current;
    if (!node) return;
    const peak = readTrackPeak(track);
    const m = level.current;
    const db = Math.max(FLOOR, toDb(peak || 0));
    m.bar = Math.max(db, m.bar - FALL_DB_S * s.dt);
    if (peak > 1 && !m.over) {
      m.over = true;
      node.classList.add("is-over");
    }
    const lv = dbToShare(m.bar).toFixed(3);
    if (node.style.getPropertyValue("--lv") !== lv) node.style.setProperty("--lv", lv);
  });
  const clear = (e) => {
    e.stopPropagation();
    level.current.over = false;
    ref.current.classList.remove("is-over");
  };
  return html`<i class="tl-meter" ref=${ref} title="這條軌道的音量；變紅代表播放時曾超過 0 dB，點一下清除" onClick=${clear}></i>`;
}

// Two thin bars for the player bar; clicking them shows or hides the panel.
export function MiniMeter({ active, title, onClick }) {
  const barRefs = [useRef(null), useRef(null)];
  useMeter((s) => {
    for (let ch = 0; ch < 2; ch += 1) {
      if (barRefs[ch].current) setBar(barRefs[ch].current, s.bars[ch], s.holds[ch]);
    }
  });
  return html`<button type="button" class=${`mini-meter ${active ? "is-active" : ""}`} title=${title}
      aria-label=${title} aria-pressed=${active} onClick=${onClick}>
    ${[0, 1].map((ch) => html`<span class="meters-bar" ref=${barRefs[ch]}><b class="meters-hold"></b></span>`)}
  </button>`;
}
