// Web Audio mix for the preview: every sounding element goes through its own gain into one stereo bus.
// The bus feeds the level meters before the player volume (so they show the mix as export.py renders it)
// and the speakers after it. Gains above 1 (+dB) are heard here, which element.volume cannot do.
// Elements of one timeline row share a track bus between their gain and the mix, so each row has its own
// level (MX-02). The mix also feeds the loudness meter (loudness-worklet.js, MX-01).
// One context for the page; it is created on first playback, since browsers start it only after a gesture.
const FFT = 2048;
const TRACK_FFT = 1024;
const SMOOTH = 0.01; // seconds; a gain change ramps this fast instead of clicking
const QUIET = 1e-8; // mean square below this (-80 dB) is silence for the correlation

let ctx = null;
let bus = null;
let monitor = null;
let analysers = null;
let monitorGain = 1;
let loudnessNode = null;
let loudness = null; // the worklet's latest reading
const routes = new WeakMap(); // element -> {src, gain, value, track}
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
  ctx = new AC();
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

// Sets an element's gain (linear, may exceed 1), routing it into the mix the first time, through the bus of
// timeline row `track`. False while the mix is not running and the element is not routed yet; the caller
// then uses element.volume instead.
export function routeGain(media, gain, track = "") {
  let r = routes.get(media);
  if (!r) {
    if (!ctx || ctx.state !== "running") return false;
    const src = ctx.createMediaElementSource(media);
    const node = ctx.createGain();
    node.gain.value = gain;
    src.connect(node);
    r = { src, gain: node, value: gain, track: null };
    routes.set(media, r);
  }
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
