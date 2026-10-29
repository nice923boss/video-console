// Sound of the preview player on the sample clock (PB-01). Every sound with a plain WAV copy (audioproxy.py; a sped
// clip's copy at its speed, P2-2) or finished stems is read in decoded blocks (lib/pcm-cache.js) and started on an
// exact sample of the Web Audio clock, so clips meet with no gap and no overlap, as the export mixes them; while it
// plays at normal speed this clock leads and the picture follows it (player.js). Sounds without such a copy (a copy
// still being made, stems that cover only part of a clip or a sped one) and every sound at J/L speeds play from
// media elements, kept in step with the timeline as before; playing backwards, the sounds of the scheduler play
// backwards in short grains (P2-4).
import { fileUrl } from "../lib/format.js";
import { audioLength, audioSpeed, audioMedia, audioTrack, VOICE_TRACK, SOURCE_TRACK } from "../lib/sequence.js";
import { valueAt, takeKeys } from "../lib/keyframes.js";
import { wakeMix, unroute, mixContext, mixInput, buildVoice, tuneVoice } from "../lib/audio-mix.js";
import { voiceSpans, duckGain, duckDb, bgmRows } from "../lib/duck.js";
import { RATE, info, failed, peek, keepOnly, loading } from "../lib/pcm-cache.js";
import { report } from "../lib/crash-report.js";
import { toFrame, speedKey, speedCopy, pieces, edgeValue, edgePoints, segKey, reverseGrain,
  bestShift } from "../lib/audio-sched.js";
import { mixPlan, stemChannels } from "../lib/voice-mix.js";
import { stemsParams } from "../lib/voice-stems.js";
import { release, seekTo, playSafe, voicePlay, trackElements, audioEntry, DRIFT_PLAYING, DRIFT_PAUSED, CUE_LEAD,
  MAX_RATE } from "./player-media.js";

const SAFETY = 0.04; // seconds ahead of the context clock a sound is started, so it never starts late
const AHEAD = 1.5; // seconds of timeline whose sound is kept scheduled
const PREFETCH = 3; // seconds past that whose blocks are loaded
const GATE_MS = 400; // longest wait at the start for the first second of sound and the picture
const RAMP = 0.003; // seconds of fade where a sound starts or stops part way
const LEVEL_STEP = 480; // frames between the level points scheduled ahead (10 ms)
const PUMP_MS = 250; // the scheduler's own timer, which keeps it fed while no frames are drawn
const PUMP_GAP_MS = 150; // the timer pumps only when no frame did for this long
const IDLE_MS = 200; // a paused playhead loads the sound under it after resting this long
const SCRUB = 0.08; // seconds of sound a paused playhead plays where it lands (P2-3)
const SCRUB_FADE = 0.005; // seconds of fade at each end of that piece
const SCRUB_LEAD = 0.02; // seconds ahead of the context clock the piece starts, so it starts whole
const SCRUB_EVERY_MS = 40; // a dragged playhead starts a new piece at most this often, the latest place winning
const SCRUB_WAIT_MS = 150; // a piece whose blocks are still loading waits for them this long at most
const GRAIN = 0.08; // seconds of each grain of the backwards sound, one starting every half of that (P2-4)
const GRAIN_AHEAD = 0.12; // seconds of those grains kept scheduled ahead of the context clock
const GRAIN_DRIFT = 0.15; // seconds the backwards sound may drift from the picture before it starts again there
const GRAIN_SHIFT = 0.005; // seconds a faster backwards grain may move to line up with the one before (WSOLA)
const LATE_MS = 2000; // a playing sound still waiting for its blocks this long is noted in app.log, once

// A video clip's own sound is heard.
export const clipSounds = (clip) => clip && clip.source === "video" && clip.take.hasAudio && !clip.shot.disabled
  && !clip.shot.audioDetached && Number(clip.shot.volume ?? 1) > 0;

// [in, out] seconds of the short fades export.py puts at each cut of a clip's own sound.
export function clipFades(clips, i) {
  const len = clips[i].length;
  return [Math.max(0.03, Math.min(i > 0 ? clips[i - 1].transition.duration : 0, len / 2)),
    Math.max(0.03, Math.min(clips[i].transition.duration, len / 2))];
}

// The shot's volume times the version's volume and hand-set keys (t in file seconds), local seconds into the clip.
export const clipLevel = (clip, local) => Number(clip.shot.volume ?? 1)
  * Number(valueAt({ keyframes: takeKeys(clip.take) }, "volume", clip.in + local * (clip.speed || 1), 1));

// view(): {project, clips, total, solo, stems, monitor} of the engine as they are now. hooks.setGain(media, gain,
// track, voice) sets an element's gain; hooks.pictureReady() is true once the picture can move; hooks.fade(t) is
// the project fade; hooks.enabled false keeps every sound on elements (the silent program window).
export function createSound(view, hooks) {
  const tracks = new Map(); // audio clip id -> its elements (player-media.js), for the sounds played by element
  const voices = new Map(); // `${segKey}@${lap}` -> one scheduled sound
  let duckSpans = []; // merged voice spans the 背景音樂 rows dip under (lib/duck.js)
  let plan = null; // id -> segment (lib/audio-sched.js) of every sound the scheduler plays
  let planOf = [];
  let anchor = null; // {C0, F0, L, loop}: context frame C0 plays timeline frame F0; L the loop length in frames
  let want = null; // timeline frame the sound starts from, while it leads
  let loopOn = false;
  let gateAt = 0;
  let held = false; // the sound has started but has not reached the speakers yet: the picture waits for it
  let lastU = -1;
  let lastT = 0;
  let timer = 0;
  let lastPump = 0;
  let idle = 0;
  let scrubbing = []; // the sound chains of the piece a paused playhead plays now
  let scrubAt = 0;
  let scrubTimer = 0;
  let backwards = null; // the backwards sound: { S, C (its next grain ends at timeline frame S, starts at context
  // frame C), chains (per sound), grains (scheduled), ends (per sound: where its last grain carried on would end) }

  const ctxOk = () => {
    const c = mixContext();
    return !!c && c.state === "running" && c.sampleRate === RATE;
  };

  function ensurePlan() {
    const v = view();
    const of = [v.project, v.clips, v.total, v.stems, v.monitor];
    if (plan && of.every((x, i) => x === planOf[i])) return plan;
    planOf = of;
    plan = buildPlan(v);
    return plan;
  }

  function buildPlan({ project, clips, total, stems, monitor }) {
    const segs = new Map();
    const flags = project.tracks || {};
    const add = (seg) => seg.f1 > seg.f0 && segs.set(seg.id, { ...seg, key: segKey(seg) });
    if (!flags.videoMuted) {
      clips.forEach((clip, i) => {
        const file = clip.take && speedCopy(clip.take, clip.speed);
        if (!clipSounds(clip) || !file) return;
        const [fi, fo] = clipFades(clips, i);
        add({ id: `c:${clip.shot.id}`, url: fileUrl(project.id, file), s0: toFrame(clip.in / Number(speedKey(clip.speed))), f0: toFrame(clip.start),
          f1: toFrame(clip.start + clip.length), fi: toFrame(fi), fo: toFrame(fo), track: SOURCE_TRACK,
          level: (t) => clipLevel(clip, t - clip.start) * hooks.fade(t) });
      });
    }
    for (const layer of project.layers || []) {
      if (layer.muted) continue;
      for (const item of project.overlays || []) {
        const media = (project.media || {})[item.mediaId];
        if (item.layer !== layer.id || item.disabled || !media || media.kind !== "video" || !media.previewAudio) continue;
        const start = Number(item.start || 0);
        add({ id: `p:${item.id}`, url: fileUrl(project.id, media.previewAudio), s0: toFrame(Number(item.in || 0)),
          f0: toFrame(start), f1: toFrame(Math.min(start + Number(item.length || 0), total)), fi: 0, fo: 0,
          track: layer.id, level: (t) => Number(valueAt(item, "volume", t - start, 1)) * hooks.fade(t) });
      }
    }
    const bgm = bgmRows(flags);
    const db = duckDb(project.settings);
    for (const item of project.audio || []) {
      const media = audioMedia(project, item);
      const track = audioTrack(item);
      if (!media || item.muted || flags[track === VOICE_TRACK ? "voiceMuted" : "musicMuted"]) continue;
      const sp = audioSpeed(item);
      const start = Number(item.start || 0);
      const tIn = Number(item.in || 0);
      const len = Math.min(audioLength(item, media), total - start);
      if (len <= 0.05) continue;
      const entry = stems[item.id];
      const s = entry && entry.stems;
      let file = speedCopy(media, sp);
      let s0 = toFrame(tIn / Number(speedKey(sp)));
      let fx = null;
      if (s && entry.made === stemsParams(media, item)) {
        // stems at another speed, or over part of it: elements
        if (speedKey(sp) !== "1" || s.from > tIn + 0.001 || s.to < tIn + len - 0.001) continue;
        file = s.file;
        s0 = toFrame(tIn - s.from);
        fx = { channels: stemChannels(item), plan: mixPlan(item), monitor: monitor && monitor.id === item.id ? monitor.mode : "fx" };
      }
      if (!file) continue;
      const ducked = bgm.includes(track);
      add({ id: `a:${item.id}`, url: fileUrl(project.id, file), s0, f0: toFrame(start),
        f1: toFrame(start + len), fi: toFrame(Math.min(Number(item.fadeIn || 0), len / 2)),
        fo: toFrame(Math.min(Number(item.fadeOut || 0), len / 2)), track, fx,
        level: (t) => Number(valueAt(item, "volume", t - start, 1)) * hooks.fade(t) * (ducked ? duckGain(duckSpans, t, db) : 1) });
    }
    return segs;
  }

  // Loads the blocks of timeline frames [from, to); true once all of them are in.
  function warm(from, to) {
    let ready = true;
    for (const seg of ensurePlan().values()) {
      if (failed(seg.url) || seg.f1 <= from || seg.f0 >= to) continue;
      const meta = info(seg.url);
      if (!meta) {
        ready = false;
        continue;
      }
      for (const p of pieces(seg, from, to, meta.frames)) if (!peek(seg.url, p.block)) ready = false;
    }
    return ready;
  }

  const at = (u) => (anchor.C0 + u - anchor.F0) / RATE; // context seconds of unrolled timeline frame u

  // One sound of the plan in loop lap `lap`, from unrolled frame fromU on: its own fades on `edge`, its level (and
  // the stems remix) after them, and `env` fading it in where it starts part way and out where it is dropped.
  function makeVoice(seg, lap, fromU) {
    const c = mixContext();
    const base = lap ? lap * anchor.L : 0;
    const u0 = Math.max(fromU, base + seg.f0);
    const env = c.createGain();
    const edge = c.createGain();
    const level = c.createGain();
    env.connect(edge);
    let mix = null;
    if (seg.fx) {
      mix = buildVoice(edge, level, seg.fx.channels);
      tuneVoice(mix, seg.fx.plan, seg.fx.monitor);
    } else {
      edge.connect(level);
    }
    level.connect(mixInput(seg.track));
    edge.gain.setValueAtTime(edgeValue(seg, u0 - base), at(u0));
    for (const [F, value] of edgePoints(seg)) if (base + F > u0) edge.gain.linearRampToValueAtTime(value, at(base + F));
    return { sk: seg.key, seg, base, u0, env, edge, mix, level, levelOf: null, levelTo: u0, sources: [], fedTo: u0,
      dry: u0 > base + seg.f0, endU: base + Math.min(seg.f1, anchor.L), waitAt: 0, lateSent: false };
  }

  const heard = (track, solo) => !solo.length || solo.includes(track);

  // Holds the value a param has at context time `when` and drops what was scheduled after it.
  function holdAt(g, when) {
    if (g.cancelAndHoldAtTime) {
      g.cancelAndHoldAtTime(when);
    } else {
      g.cancelScheduledValues(when);
      g.setValueAtTime(g.value, when);
    }
  }

  // Schedules the level of a sound up to unrolled frame toU as straight lines between points LEVEL_STEP frames apart
  // (export steps its volume once per audio frame); after an edit or a solo the points from fromU on are redone.
  function automate(v, fromU, toU, on) {
    const g = v.level.gain;
    const levelAt = (u) => (on ? v.seg.level((u - v.base) / RATE) : 0);
    if (v.levelOf !== v.seg || v.on !== on || v.levelTo < Math.min(fromU, v.endU)) {
      const from = Math.max(fromU, v.u0);
      if (v.levelTo > from) holdAt(g, at(from));
      else g.setValueAtTime(levelAt(from), at(from));
      v.levelTo = from;
      v.levelOf = v.seg;
      v.on = on;
    }
    const end = Math.min(toU, v.endU);
    while (v.levelTo < end) {
      const u = Math.min(end, v.base + (Math.floor((v.levelTo - v.base) / LEVEL_STEP) + 1) * LEVEL_STEP);
      g.linearRampToValueAtTime(levelAt(u), at(u));
      v.levelTo = u;
    }
  }

  function unplug(v) {
    v.sources.forEach((s) => s.node.disconnect());
    v.env.disconnect();
    v.edge.disconnect();
    if (v.mix) v.mix.merge.disconnect();
    v.level.disconnect();
  }

  // Fades the sound out from context time `when` on and stops what was scheduled after it.
  function dropVoice(v, when) {
    const c = mixContext();
    const g = v.env.gain;
    holdAt(g, when);
    g.linearRampToValueAtTime(0, when + RAMP);
    for (const s of v.sources) {
      const stopAt = Math.max(s.begin, when + RAMP);
      if (stopAt < s.end) s.node.stop(stopAt);
    }
    setTimeout(() => unplug(v), (when + RAMP - c.currentTime) * 1000 + 200);
  }

  function dropAll() {
    const c = mixContext();
    for (const v of voices.values()) dropVoice(v, c.currentTime);
    voices.clear();
  }

  // Schedules the decoded blocks of a sound up to unrolled frame toU (a block that is not in yet waits for a later
  // pump; the sound then fades in where it catches up).
  function feed(v, fromU, toU) {
    const meta = info(v.seg.url);
    if (!meta) return late(v, -1);
    if (v.fedTo >= Math.min(toU, v.endU)) return;
    if (v.fedTo < fromU) {
      v.fedTo = fromU;
      v.dry = true;
    }
    const c = mixContext();
    for (const p of pieces(v.seg, v.fedTo - v.base, v.endU - v.base, meta.frames)) {
      if (v.base + p.F >= toU) return;
      const buf = peek(v.seg.url, p.block);
      if (!buf) {
        v.fedTo = v.base + p.F;
        return late(v, p.block);
      }
      v.waitAt = 0;
      const C = anchor.C0 + v.base + p.F - anchor.F0;
      const begin = C / RATE;
      const end = (C + p.n) / RATE;
      const node = c.createBufferSource();
      node.buffer = buf;
      node.connect(v.env);
      if (v.dry) {
        v.env.gain.setValueAtTime(0, begin);
        v.env.gain.linearRampToValueAtTime(1, begin + RAMP);
        v.dry = false;
      }
      node.start(begin, p.offset / RATE);
      node.stop(end);
      v.sources.push({ node, begin, end });
      v.fedTo = v.base + p.F + p.n;
    }
    v.fedTo = v.endU;
  }

  // A sound waiting for its file's header (block -1) or a block; one still waiting LATE_MS after it was first due is
  // noted in app.log (once per sound), so a silence in the preview can be traced afterwards.
  function late(v, block) {
    const now = performance.now();
    if (!v.waitAt) v.waitAt = now;
    if (v.lateSent || now - v.waitAt < LATE_MS) return;
    v.lateSent = true;
    report("預覽聲音遲到", `${v.seg.url} ${block < 0 ? "檔頭" : `區塊 ${block}`}，已等 ${Math.round(now - v.waitAt)} 毫秒，`
      + `讀取中 ${loading()} 個區塊`);
  }

  function pump() {
    const c = mixContext();
    lastPump = performance.now();
    const { F0, C0, L } = anchor;
    const nowU = F0 + c.currentTime * RATE - C0;
    const fromU = Math.max(F0, Math.ceil(nowU + SAFETY * RATE));
    const toU = Math.floor(nowU + AHEAD * RATE);
    const segs = ensurePlan();
    const { solo } = view();
    for (const [key, v] of voices) {
      const seg = segs.get(v.seg.id);
      if (!seg || seg.key !== v.sk || failed(seg.url)) {
        dropVoice(v, at(fromU));
        voices.delete(key);
      } else if (v.endU <= nowU) {
        unplug(v);
        voices.delete(key);
      } else {
        v.seg = seg;
      }
    }
    const lap0 = L === Infinity ? 0 : Math.floor(fromU / L);
    const lap1 = L === Infinity ? 0 : Math.floor(toU / L);
    for (const seg of segs.values()) {
      if (failed(seg.url)) continue;
      for (let lap = lap0; lap <= lap1; lap += 1) {
        const base = lap ? lap * L : 0;
        const key = `${seg.key}@${lap}`;
        if (base + Math.min(seg.f1, L) <= fromU || base + seg.f0 >= toU || voices.has(key)) continue;
        voices.set(key, makeVoice(seg, lap, fromU));
      }
    }
    for (const v of voices.values()) {
      feed(v, fromU, toU);
      automate(v, fromU, toU, heard(v.seg.track, solo));
      if (v.mix) tuneVoice(v.mix, v.seg.fx.plan, v.seg.fx.monitor);
      v.sources = v.sources.filter((s) => {
        if (s.end > c.currentTime) return true;
        s.node.disconnect();
        return false;
      });
    }
    const ahead = L === Infinity ? toU : toU % L;
    const far = ahead + PREFETCH * RATE;
    warm(ahead, far);
    if (far > L) warm(0, far - L);
  }

  // Opens once the first second of sound is in and the picture is ready, or after GATE_MS whatever is missing.
  function openGate() {
    if (!ctxOk()) return;
    const ready = warm(want, want + RATE);
    if (!(performance.now() - gateAt > GATE_MS || (ready && hooks.pictureReady()))) return;
    const c = mixContext();
    anchor = { C0: Math.ceil((c.currentTime + SAFETY) * RATE), F0: want, L: loopOn ? toFrame(view().total) : Infinity,
      loop: loopOn };
    held = true;
    pump();
  }

  function onTimer() {
    if (!anchor) openGate();
    else if (performance.now() - lastPump > PUMP_GAP_MS) pump();
  }

  // Fades out the piece a paused playhead plays and drops the one waiting to play.
  function cutScrub() {
    clearTimeout(scrubTimer);
    if (!scrubbing.length) return;
    const now = mixContext().currentTime;
    for (const s of scrubbing) {
      holdAt(s.env.gain, now);
      s.env.gain.linearRampToValueAtTime(0, now + RAMP);
      for (const p of s.nodes) if (now + RAMP < p.end) p.node.stop(now + RAMP);
    }
    scrubbing = [];
  }

  // Plays SCRUB seconds of every sound heard at timeline seconds t, where the paused playhead moved (P2-3),
  // cutting off the piece before; a piece whose blocks are still loading waits for them up to SCRUB_WAIT_MS.
  function scrub(t, since = performance.now()) {
    clearTimeout(scrubTimer);
    if (hooks.enabled === false || want != null || !ctxOk()) return;
    const wait = SCRUB_EVERY_MS - (performance.now() - scrubAt);
    if (wait > 0) {
      scrubTimer = setTimeout(() => scrub(t, since), wait);
      return;
    }
    const F = toFrame(t);
    const { solo } = view();
    const found = [];
    let missing = false;
    for (const seg of ensurePlan().values()) {
      if (failed(seg.url) || seg.f0 > F || seg.f1 <= F || !heard(seg.track, solo)) continue;
      const meta = info(seg.url);
      const parts = meta ? pieces(seg, F, F + toFrame(SCRUB), meta.frames).map((p) => ({ ...p, buf: peek(seg.url, p.block) })) : [];
      if (!meta || parts.some((p) => !p.buf)) missing = true;
      else if (parts.length) found.push({ seg, parts });
    }
    if (missing && performance.now() - since < SCRUB_WAIT_MS) {
      scrubTimer = setTimeout(() => scrub(t, since), 20);
      return;
    }
    cutScrub();
    scrubAt = performance.now();
    const c = mixContext();
    const t0 = c.currentTime + SCRUB_LEAD;
    for (const { seg, parts } of found) {
      const end = parts[parts.length - 1];
      const a = t0 + (parts[0].F - F) / RATE;
      const b = t0 + (end.F + end.n - F) / RATE;
      if (b - a < 4 * SCRUB_FADE) continue;
      const env = c.createGain();
      const level = c.createGain();
      let mix = null;
      if (seg.fx) {
        mix = buildVoice(env, level, seg.fx.channels);
        tuneVoice(mix, seg.fx.plan, seg.fx.monitor);
      } else {
        env.connect(level);
      }
      level.gain.value = edgeValue(seg, F) * seg.level(t);
      level.connect(mixInput(seg.track));
      env.gain.value = 0;
      env.gain.setValueAtTime(0, a);
      env.gain.linearRampToValueAtTime(1, a + SCRUB_FADE);
      env.gain.setValueAtTime(1, b - SCRUB_FADE);
      env.gain.linearRampToValueAtTime(0, b);
      const nodes = parts.map((p) => {
        const node = c.createBufferSource();
        node.buffer = p.buf;
        node.connect(env);
        const begin = t0 + (p.F - F) / RATE;
        node.start(begin, p.offset / RATE);
        node.stop(begin + p.n / RATE);
        return { node, end: begin + p.n / RATE };
      });
      scrubbing.push({ env, nodes });
      setTimeout(() => {
        nodes.forEach((p) => p.node.disconnect());
        env.disconnect();
        if (mix) mix.merge.disconnect();
        level.disconnect();
      }, (b - c.currentTime) * 1000 + 200);
    }
  }

  // Fades out the backwards sound and drops its grains still to play.
  function cutBackwards() {
    if (!backwards) return;
    const now = mixContext().currentTime;
    const { chains, grains } = backwards;
    for (const ch of chains.values()) {
      holdAt(ch.env.gain, now);
      ch.env.gain.linearRampToValueAtTime(0, now + RAMP);
    }
    for (const g of grains) if (now + RAMP < g.end) g.node.stop(now + RAMP);
    setTimeout(() => chains.forEach((ch) => {
      ch.env.disconnect();
      if (ch.mix) ch.mix.merge.disconnect();
      ch.level.disconnect();
    }), RAMP * 1000 + 200);
    backwards = null;
  }

  // The sound chain a sound's backwards grains play into: its stems remix and its track, as scrub's.
  function chainOf(key, seg) {
    let ch = backwards.chains.get(key);
    if (ch) return ch;
    const c = mixContext();
    ch = { env: c.createGain(), level: c.createGain(), mix: null };
    if (seg.fx) {
      ch.mix = buildVoice(ch.env, ch.level, seg.fx.channels);
      tuneVoice(ch.mix, seg.fx.plan, seg.fx.monitor);
    } else {
      ch.env.connect(ch.level);
    }
    ch.level.connect(mixInput(seg.track));
    backwards.chains.set(key, ch);
    return ch;
  }

  // Channel 0 of a sound over timeline frames [a, b), silence outside it; null while a block is still loading.
  function frames0(seg, meta, a, b) {
    const x = new Float32Array(b - a);
    for (const p of pieces(seg, a, b, meta.frames)) {
      const buf = peek(seg.url, p.block);
      if (!buf) return null;
      x.set(buf.getChannelData(0).subarray(p.offset, p.offset + p.n), p.F - a);
    }
    return x;
  }

  // One grain of every sound heard: timeline frames [S - n, S) backwards, from context frame C on. Faster than -1x,
  // a sound's grain moves up to GRAIN_SHIFT to start like its last one carried on, so a steady tone keeps its level
  // (WSOLA). A sound whose blocks are still loading is left out of it.
  function grain(S, C, n) {
    const c = mixContext();
    const { solo } = view();
    const hop = n / 2;
    const tol = toFrame(GRAIN_SHIFT);
    for (const [key, seg] of ensurePlan()) {
      const E = backwards.ends.get(key);
      backwards.ends.delete(key);
      if (failed(seg.url) || seg.f0 >= S + tol || seg.f1 <= S - n - tol || !heard(seg.track, solo)) continue;
      const meta = info(seg.url);
      let A = S;
      if (meta && E != null && E !== S) {
        const a = Math.min(S - tol - hop, E - hop);
        const x = frames0(seg, meta, a, Math.max(S + tol, E));
        if (x) A += bestShift(x, a, E, S, hop, tol, 4);
      }
      const parts = meta ? pieces(seg, A - n, A, meta.frames).map((p) => ({ ...p, buf: peek(seg.url, p.block) })) : [];
      const v = edgeValue(seg, A - n / 2) * seg.level((A - n / 2) / RATE);
      if (!parts.length || parts.some((p) => !p.buf) || !(v > 0)) continue;
      const buf = new AudioBuffer({ length: n, numberOfChannels: parts[0].buf.numberOfChannels, sampleRate: RATE });
      const out = Array.from({ length: buf.numberOfChannels }, (_, ch) => buf.getChannelData(ch));
      reverseGrain(out, parts.map((p) => ({ ...p, data: out.map((_, ch) => p.buf.getChannelData(ch)) })), A, n, v);
      const node = c.createBufferSource();
      node.buffer = buf;
      node.connect(chainOf(key, seg).env);
      node.start(C / RATE);
      node.onended = () => node.disconnect();
      backwards.grains.push({ node, end: (C + n) / RATE });
      backwards.ends.set(key, A - hop);
    }
  }

  // Playing backwards at clock rate `rate` (< 0), at timeline seconds t now: the sound plays backwards with the
  // picture (P2-4), in grains of GRAIN under a Hann window, one every half of that, which add back up to the sound
  // at -1x. Faster, each grain still plays at its own pitch and the timeline between them is skipped. t null stops it.
  function reverse(t, rate) {
    if (t == null || hooks.enabled === false || want != null) {
      cutBackwards();
      return;
    }
    wakeMix();
    if (!ctxOk()) return;
    const c = mixContext();
    const n = toFrame(GRAIN);
    const hop = n / 2;
    const now = Math.ceil(c.currentTime * RATE);
    const late = toFrame(c.outputLatency || c.baseLatency || 0);
    // The grain starting at context frame C ends at the timeline frame that puts its middle under the picture's.
    const due = (C) => toFrame(t) + rate * (C + late + hop - now) + hop;
    if (backwards && backwards.C > now && Math.abs(backwards.S - due(backwards.C)) <= toFrame(GRAIN_DRIFT) * -rate) {
      backwards.grains = backwards.grains.filter((g) => g.end > c.currentTime);
    } else {
      cutBackwards();
      const C = now + toFrame(SCRUB_LEAD);
      backwards = { S: Math.round(due(C)), C, chains: new Map(), grains: [], ends: new Map() };
    }
    warm(backwards.S + rate * RATE - toFrame(GRAIN_SHIFT), backwards.S + toFrame(GRAIN_SHIFT));
    while (backwards.C < now + toFrame(GRAIN_AHEAD)) {
      grain(backwards.S, backwards.C, n);
      backwards.S += Math.round(rate * hop);
      backwards.C += hop;
    }
  }

  // Starts the sound at timeline seconds t (loop: it wraps at the end of the sequence).
  function start(t, loop) {
    cutScrub();
    cutBackwards();
    if (mixContext()) dropAll();
    loopOn = !!loop;
    want = toFrame(t);
    if (loopOn && want >= toFrame(view().total)) want = 0;
    anchor = null;
    gateAt = performance.now();
    lastU = -1;
    lastT = t;
    if (!timer) timer = setInterval(onTimer, PUMP_MS);
  }

  function stop() {
    if (mixContext()) dropAll();
    anchor = null;
    want = null;
    clearInterval(timer);
    timer = 0;
  }

  // Timeline seconds heard at rAF time `now`, or null while the start waits (the picture then holds).
  function time(now, loop) {
    if (want == null || !!loop !== loopOn || (anchor && anchor.loop && anchor.L !== toFrame(view().total))) start(lastT, loop);
    if (!anchor) openGate();
    if (!anchor) return null;
    pump();
    const c = mixContext();
    const ts = c.getOutputTimestamp ? c.getOutputTimestamp() : null;
    const out = ts && ts.contextTime > 0 && ts.performanceTime > 0
      ? ts.contextTime + (now - ts.performanceTime) / 1000
      : c.currentTime - (c.outputLatency || c.baseLatency || 0);
    const u = anchor.F0 + out * RATE - anchor.C0;
    held = u < anchor.F0;
    if (held) return anchor.F0 / RATE;
    lastU = Math.max(lastU, u);
    lastT = (anchor.L === Infinity ? lastU : lastU % anchor.L) / RATE;
    return lastT;
  }

  // While paused, the sound under the playhead is loaded, so playing starts without waiting.
  function prefetchIdle(t) {
    if (hooks.enabled === false) return;
    clearTimeout(idle);
    idle = setTimeout(() => warm(toFrame(t), toFrame(t) + RATE), IDLE_MS);
  }

  const owns = (key) => {
    const seg = plan && plan.get(key);
    return !!seg && !failed(seg.url);
  };

  function drawElements(t, playing, lead, rate) {
    const { project, total, solo, stems, monitor } = view();
    const c = mixContext();
    const mixOn = !!c && c.state === "running";
    // The scheduler plays these, or will once playing starts: their elements let go of their files.
    const ready = lead || (!playing && ctxOk() && hooks.enabled !== false);
    const g = hooks.fade(t);
    const flags = project.tracks || {};
    const bgm = bgmRows(flags);
    const db = duckDb(project.settings);
    for (const tr of tracks.values()) {
      const { item, media } = tr;
      if (ready && owns(`a:${item.id}`)) {
        for (const el of trackElements(tr)) if (el.hasAttribute("src")) release(el);
        continue;
      }
      const start = Number(item.start || 0);
      const len = Math.min(audioLength(item, media), total - start);
      const local = t - start;
      const track = audioTrack(item);
      const off = item.muted || flags[track === VOICE_TRACK ? "voiceMuted" : "musicMuted"] || (solo.length && !solo.includes(track));
      const sp = audioSpeed(item);
      const speed = Math.min(MAX_RATE, rate * sp);
      const mode = monitor && monitor.id === item.id ? monitor.mode : "fx";
      if (!playing || off || len <= 0.05 || local < 0 || local >= len) {
        for (const el of trackElements(tr)) if (!el.paused) el.pause();
        // Put a clip about to start, or the one under a paused playhead, in place now: seeking into a long
        // camera file takes a few hundred ms, heard as a gap at the cut when it happens on the clip's first frame.
        // Any other lets go of its file (CUE_LEAD).
        if (!off && len > 0.05 && local < len && local > -CUE_LEAD * speed) {
          const cue = voicePlay(tr, Number(item.in || 0) + Math.max(0, local) * sp, project.id, mixOn && stems[item.id], mode);
          if (Math.abs(cue.el.currentTime - cue.at) > DRIFT_PAUSED) seekTo(cue.el, cue.holder, cue.at);
        } else {
          for (const el of trackElements(tr)) if (el.hasAttribute("src")) release(el);
        }
        continue;
      }
      const fi = Math.min(Number(item.fadeIn || 0), len / 2);
      const fo = Math.min(Number(item.fadeOut || 0), len / 2);
      let gain = Number(valueAt(item, "volume", local, 1)) * g;
      if (fi > 0 && local < fi) gain *= local / fi;
      if (fo > 0 && local > len - fo) gain *= (len - local) / fo;
      if (bgm.includes(track)) gain *= duckGain(duckSpans, t, db);
      const target = Number(item.in || 0) + local * sp;
      const { el, holder, at: elAt, voice: fx } = voicePlay(tr, target, project.id, mixOn && stems[item.id], mode);
      hooks.setGain(el, gain, track, fx);
      if (el.playbackRate !== speed) el.playbackRate = speed;
      if (Math.abs(el.currentTime - elAt) > DRIFT_PLAYING * Math.max(1, speed)) seekTo(el, holder, elAt);
      if (el.paused) playSafe(el);
    }
  }

  return {
    // True when the sample clock leads at this clock rate: normal speed forward, with the mix running at 48 kHz.
    leads(rate) {
      return hooks.enabled !== false && rate === 1 && wakeMix() && mixContext().sampleRate === RATE;
    },
    owns,
    start,
    time,
    scrub,
    reverse,
    // lead: the sample clock leads (see leads). Returns whether the picture moves: not while the start waits, nor
    // until its first sound reaches the speakers (the picture would start that much ahead of it).
    draw(t, playing, lead, rate) {
      ensurePlan();
      if (playing) wakeMix();
      if (lead && want == null) start(t, loopOn);
      if (!lead) {
        if (want != null) stop();
        prefetchIdle(t);
      }
      const moving = playing && !(lead && (!anchor || held));
      drawElements(t, moving, lead, rate);
      return moving;
    },
    // After the project changed: the elements of the audio clips, the voice spans and the cached files.
    sync() {
      const { project, clips, stems } = view();
      duckSpans = voiceSpans(project, clips.filter(clipSounds).map((c) => [c.start, c.start + c.length]));
      const seen = new Set();
      for (const item of project.audio || []) {
        const media = audioMedia(project, item);
        if (!media) continue;
        seen.add(item.id);
        let tr = tracks.get(item.id);
        if (!tr) {
          tr = audioEntry();
          tracks.set(item.id, tr);
        }
        const src = fileUrl(project.id, media.file);
        if (tr.src !== src) {
          tr.src = src;
          release(tr.audio); // voicePlay loads it once the clip is near the playhead
        }
        tr.item = item;
        tr.media = media;
      }
      for (const [id, tr] of tracks) {
        if (seen.has(id)) continue;
        for (const audio of trackElements(tr)) {
          release(audio);
          unroute(audio);
        }
        tracks.delete(id);
      }
      const urls = new Set();
      const keep = (file) => file && urls.add(fileUrl(project.id, file));
      for (const x of [...Object.values(project.takes || {}), ...Object.values(project.media || {})]) {
        keep(x.previewAudio);
        Object.values(x.previewAudioSpeeds || {}).forEach(keep);
      }
      Object.values(stems || {}).forEach((e) => keep(e && e.stems && e.stems.file));
      keepOnly(urls);
    },
    hold() {
      for (const tr of tracks.values()) for (const el of trackElements(tr)) if (!el.paused) el.pause();
    },
    destroy() {
      stop();
      cutScrub();
      cutBackwards();
      clearTimeout(idle);
      tracks.forEach((tr) => trackElements(tr).forEach((media) => {
        release(media);
        unroute(media);
      }));
      tracks.clear();
    },
  };
}
