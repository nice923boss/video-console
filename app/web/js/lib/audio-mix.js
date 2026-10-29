// Web Audio mix for the preview: every sounding element goes through its own gain into one stereo bus.
// The bus feeds the level meters before the player volume (so they show the mix as export.py renders it)
// and the speakers after it. Gains above 1 (+dB) are heard here, which element.volume cannot do.
// Elements of one timeline row share a track bus between their gain and the mix, so each row has its own
// level (MX-02). The mix also feeds the loudness meter (loudness-worklet.js, MX-01).
// One context for the page; it is created on first playback, since browsers start it only after a gesture. It runs
// at 48 kHz, the rate the export mixes at and the preview's sound copies are made at (PB-01).
import { LOW_HZ, HIGH_HZ, BASS_HZ, BODY_HZ, BODY_Q, TONE_HZ } from "./voice-mix.js";

const FFT = 2048;
const TRACK_FFT = 1024;
const SMOOTH = 0.01; // seconds; a gain change ramps this fast instead of clicking
const QUIET = 1e-8; // mean square below this (-80 dB) is silence for the correlation
const LP_Q_DB = 20 * Math.log10(Math.SQRT1_2); // Butterworth; a Web Audio low pass takes its Q in dB

let ctx = null;
let bus = null;
let monitor = null;
let analysers = null;
let monitorGain = 1;
let loudnessNode = null;
let loudness = null; // the worklet's latest reading
const routes = new WeakMap(); // element -> {src, gain, value, track, c, voice}
const trackBuses = new Map(); // row key -> {input, analysers}
const bufs = [new Float32Array(FFT), new Float32Array(FFT)];
const trackBuf = new Float32Array(TRACK_FFT);

function splitInto(node, size) {
  const split = ctx.createChannelSplitter(2);
  node.connect(split);
  return [0, 1].map((ch) => {
    const a = ctx.createAnalyser();
    a.fftSize = size;
    split.connect(a, ch);
    return a;
  });
}

function startLoudness() {
  if (!ctx.audioWorklet || typeof AudioWorkletNode === "undefined") return;
  ctx.audioWorklet.addModule(new URL("./loudness-worklet.js", import.meta.url)).then(() => {
    loudnessNode = new AudioWorkletNode(ctx, "vc-loudness",
      { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 2, channelCountMode: "explicit" });
    loudnessNode.port.onmessage = (e) => { loudness = e.data; };
    bus.connect(loudnessNode);
    loudnessNode.connect(ctx.destination); // its output stays silent; being connected keeps it running everywhere
  }).catch((err) => console.warn("無法啟動響度表", err));
}

function ensure() {
  if (ctx) return ctx;
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  ctx = new AC({ sampleRate: 48000 });
  bus = ctx.createGain();
  bus.channelCount = 2;
  bus.channelCountMode = "explicit";
  bus.channelInterpretation = "speakers";
  analysers = splitInto(bus, FFT);
  monitor = ctx.createGain();
  monitor.gain.value = monitorGain;
  bus.connect(monitor);
  monitor.connect(ctx.destination);
  startLoudness();
  return ctx;
}

function trackBus(key) {
  let t = trackBuses.get(key);
  if (!t) {
    const input = ctx.createGain();
    input.channelCount = 2;
    input.channelCountMode = "explicit";
    input.channelInterpretation = "speakers";
    input.connect(bus);
    t = { input, analysers: splitInto(input, TRACK_FFT) };
    trackBuses.set(key, t);
  }
  return t;
}

// Called while playing: creates or resumes the context. True once elements can be routed through it.
export function wakeMix() {
  const c = ensure();
  if (!c) return false;
  if (c.state === "suspended") c.resume().catch(() => {});
  return c.state === "running";
}

function gainNode(value = 1) {
  const node = ctx.createGain();
  node.gain.value = value;
  return node;
}

function biquadNode(type, hz, q = null) {
  const node = ctx.createBiquadFilter();
  node.type = type;
  node.frequency.value = hz;
  if (q !== null) node.Q.value = q;
  return node;
}

// The remix of a stems element (voice-mix.js mixPlan, as voiceclean.mix_lines renders it for the export): its 3c
// channels are the source X, the denoised D and V, D with the echo taken out. Per side Y = gh*X + (gr-gh)*D +
// (1-gr)*V + (gm-gh)*LP4k(X-D) + (gl-gm)*LP250(X-D), then the EQ; one picked channel (c 1) plays on both sides.
// The gains start at 0 and are set by tuneVoice.
export function buildVoice(src, out, c) {
  const split = ctx.createChannelSplitter(3 * c);
  src.connect(split);
  const merge = ctx.createChannelMerger(2);
  const sides = [];
  for (let o = 0; o < c; o += 1) {
    const at = (stem) => stem * c + o;
    const pre = gainNode(); // Y before the EQ
    const [x, d, v] = [0, 1, 2].map((stem) => {
      const g = gainNode(0);
      split.connect(g, at(stem));
      g.connect(pre);
      return g;
    });
    const noise = gainNode(); // X - D
    const minusD = gainNode(-1);
    split.connect(noise, at(0));
    split.connect(minusD, at(1));
    minusD.connect(noise);
    const bands = [LOW_HZ, HIGH_HZ].map((hz) => {
      const lp = biquadNode("lowpass", hz, LP_Q_DB);
      const g = gainNode(0);
      noise.connect(lp);
      lp.connect(g);
      g.connect(pre);
      return g;
    });
    const eq = [biquadNode("lowshelf", BASS_HZ), biquadNode("peaking", BODY_HZ, BODY_Q), biquadNode("highshelf", TONE_HZ)];
    pre.connect(eq[0]);
    eq[0].connect(eq[1]);
    eq[1].connect(eq[2]);
    const removed = gainNode(); // X - Y before the EQ: what the cleanup takes out
    const minusPre = gainNode(-1);
    split.connect(removed, at(0));
    pre.connect(minusPre);
    minusPre.connect(removed);
    const fx = gainNode(0);
    const dry = gainNode(0);
    const delta = gainNode(0);
    eq[2].connect(fx);
    split.connect(dry, at(0));
    removed.connect(delta);
    for (const node of [fx, dry, delta]) {
      for (const side of c === 1 ? [0, 1] : [o]) node.connect(merge, 0, side);
    }
    sides.push({ x, d, v, bands, eq, fx, dry, delta });
  }
  merge.connect(out);
  return { c, merge, sides, key: "" };
}

// plan: mixPlan of the clip; monitor: what is heard, "fx" the cleaned sound, "dry" the source as it is (unity, not
// level-matched), "delta" only what the cleanup takes out.
export function tuneVoice(voice, plan, monitor) {
  const key = JSON.stringify([plan, monitor]);
  if (voice.key === key) return;
  const now = !voice.key;
  voice.key = key;
  const set = (param, value) => {
    if (now) param.value = value;
    else param.setTargetAtTime(value, ctx.currentTime, SMOOTH);
  };
  const db = Object.fromEntries(plan.eq.map(([kind, , gain]) => [kind, gain]));
  for (const s of voice.sides) {
    set(s.x.gain, plan.gh);
    set(s.d.gain, plan.gr - plan.gh);
    set(s.v.gain, 1 - plan.gr);
    set(s.bands[0].gain, plan.gl - plan.gm);
    set(s.bands[1].gain, plan.gm - plan.gh);
    s.eq.forEach((f) => set(f.gain, db[f.type] || 0));
    set(s.fx.gain, monitor === "fx" ? 1 : 0);
    set(s.dry.gain, monitor === "dry" ? 1 : 0);
    set(s.delta.gain, monitor === "delta" ? 1 : 0);
  }
}

// The context (null before the first playback) and where a sound of timeline row `track` joins the mix: the row's
// bus, or the mix itself for no row. For the sample scheduler (components/player-audio.js).
export const mixContext = () => ctx;
export const mixInput = (track = "") => (ctx ? (track ? trackBus(track).input : bus) : null);

// Sets an element's gain (linear, may exceed 1), routing it into the mix the first time, through the bus of
// timeline row `track`. False while the mix is not running and the element is not routed yet; the caller
// then uses element.volume instead. voice {channels, plan, monitor}: the element plays stems (voiceclean.py) of
// that many channels per stem, remixed by buildVoice.
export function routeGain(media, gain, track = "", voice = null) {
  let r = routes.get(media);
  if (!r) {
    if (!ctx || ctx.state !== "running") return false;
    const src = ctx.createMediaElementSource(media);
    const node = ctx.createGain();
    node.gain.value = gain;
    r = { src, gain: node, value: gain, track: null, c: -1, voice: null };
    routes.set(media, r);
  }
  const c = voice ? voice.channels : 0;
  if (r.c !== c) {
    r.src.disconnect();
    if (r.voice) r.voice.merge.disconnect();
    r.voice = c ? buildVoice(r.src, r.gain, c) : null;
    if (!c) r.src.connect(r.gain);
    r.c = c;
  }
  if (voice) tuneVoice(r.voice, voice.plan, voice.monitor);
  if (r.track !== track) {
    if (r.track !== null) r.gain.disconnect();
    r.gain.connect(track ? trackBus(track).input : bus);
    r.track = track;
  }
  if (r.value !== gain) {
    r.value = gain;
    r.gain.gain.setTargetAtTime(gain, ctx.currentTime, SMOOTH);
  }
  return true;
}

// For elements that are being thrown away (an element can be routed only once in its life).
export function unroute(media) {
  const r = routes.get(media);
  if (!r) return;
  r.src.disconnect();
  if (r.voice) r.voice.merge.disconnect();
  r.gain.disconnect();
  routes.delete(media);
}

// The player's own volume slider and mute: what reaches the speakers, not what the meters show.
export function setMonitor(level, muted) {
  monitorGain = muted ? 0 : level;
  if (monitor) monitor.gain.setTargetAtTime(monitorGain, ctx.currentTime, SMOOTH);
}

function peakOf(a, buf) {
  a.getFloatTimeDomainData(buf);
  let peak = 0;
  for (let i = 0; i < buf.length; i += 1) {
    const v = Math.abs(buf[i]);
    if (v > peak) peak = v;
  }
  return peak;
}

// Over the last ~40 ms: the peak sample level (linear) of the left and right channel, and how alike the two
// channels are (MX-09): +1 the same, 0 unrelated or one side silent, -1 opposite (cancels when played in mono);
// null when both are silent. Null without a running mix.
export function readMix() {
  if (!analysers || ctx.state !== "running") return null;
  const peaks = analysers.map((a, ch) => peakOf(a, bufs[ch]));
  const [l, r] = bufs;
  let lr = 0;
  let ll = 0;
  let rr = 0;
  for (let i = 0; i < FFT; i += 1) {
    lr += l[i] * r[i];
    ll += l[i] * l[i];
    rr += r[i] * r[i];
  }
  const quiet = QUIET * FFT;
  let corr = null;
  if (ll > quiet && rr > quiet) corr = lr / Math.sqrt(ll * rr);
  else if (ll > quiet || rr > quiet) corr = 0;
  return { peaks, corr };
}

// Peak level (linear, louder channel) of one timeline row over the last ~20 ms; null before it has sounded.
export function readTrackPeak(track) {
  const t = trackBuses.get(track);
  if (!t || ctx.state !== "running") return null;
  return Math.max(peakOf(t.analysers[0], trackBuf), peakOf(t.analysers[1], trackBuf));
}

// {momentary, shortTerm, integrated, truePeak} of the mix since the last reset, or null before the first reading.
export function readLoudness() {
  return loudness;
}

export function resetLoudness() {
  loudness = null;
  if (loudnessNode) loudnessNode.port.postMessage("reset");
}
