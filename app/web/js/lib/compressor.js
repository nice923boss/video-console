// Clip compressor after the Logic Compressor in Final Cut Pro. Mirror of vc/compressor.py: the gain is worked out
// once per BLOCK samples by the same formulas, so the preview (compressor-worklet.js) and the export agree.
// Pure and streaming: the worklet feeds it 128-sample quanta, node tests feed it whole buffers.
export const RATE = 48000;
export const BLOCK = 32;
export const LOOKAHEAD = 240; // 5 ms; the preview hears the clip this much late, the export does not
export const KNEE_MAX_DB = 20;
export const RMS_MS = 10;
export const LIMIT_RELEASE_MS = 50;
export const AUTO_AVG_MS = 1000;
export const FLOOR = 1e-9;

export const CIRCUITS = [
  { key: "platinum", feedback: false, rms: null, kneeMin: 0, color: 0, kind: "odd", autoK: 0 },
  { key: "studio_vca", feedback: false, rms: true, kneeMin: 0, color: 0.08, kind: "even", autoK: 0 },
  { key: "studio_fet", feedback: true, rms: false, kneeMin: 0, color: 0.15, kind: "odd", autoK: 0 },
  { key: "classic_vca", feedback: false, rms: true, kneeMin: 0, color: 0.05, kind: "even", autoK: 2 },
  { key: "vintage_vca", feedback: false, rms: true, kneeMin: 0.4, color: 0.12, kind: "even", autoK: 1 },
  { key: "vintage_fet", feedback: true, rms: false, kneeMin: 0, color: 0.25, kind: "odd", autoK: 0 },
  { key: "vintage_opto", feedback: true, rms: true, kneeMin: 0.6, color: 0.1, kind: "even", autoK: 6 },
];

export const DEFAULT_COMP = Object.freeze({
  on: true, circuit: 0, threshold: -20, ratio: 2, knee: 0.5, attack: 15, release: 100, autoRelease: false,
  makeup: 0, autoGain: 0, rms: false, link: "max", lookahead: false, limiter: false, limiterThreshold: 0,
  distortion: "off", mix: 1, inGain: 0, outGain: 0,
});
const RANGES = {
  threshold: [-60, 0], ratio: [1, 30], knee: [0, 1], attack: [0, 200], release: [5, 5000], makeup: [-30, 30],
  limiterThreshold: [-20, 0], mix: [0, 1], inGain: [-30, 30], outGain: [-30, 30],
};
const DISTORTIONS = ["off", "soft", "hard", "clip"];

export function normalize(raw) {
  const p = { ...DEFAULT_COMP, ...(raw || {}) };
  for (const [k, [lo, hi]] of Object.entries(RANGES)) p[k] = Math.min(hi, Math.max(lo, Number(p[k])));
  p.circuit = CIRCUITS[Math.trunc(p.circuit)] ? Math.trunc(p.circuit) : 0;
  p.autoGain = [0, 1, 2].includes(Math.trunc(p.autoGain)) ? Math.trunc(p.autoGain) : 0;
  p.link = p.link === "sum" ? "sum" : "max";
  p.distortion = DISTORTIONS.includes(p.distortion) ? p.distortion : "off";
  return p;
}

const db = (x) => 20 * Math.log10(Math.max(x, FLOOR));
export const kneeDb = (p) => KNEE_MAX_DB * Math.max(p.knee, CIRCUITS[p.circuit].kneeMin);
export const coef = (ms) => Math.exp(-BLOCK / (Math.max(ms, 0.01) * 1e-3 * RATE));

export function gainComputer(x, t, r, w) {
  const over = x - t;
  if (2 * over < -w) return x;
  if (w > 0 && 2 * Math.abs(over) <= w) return x + ((1 / r - 1) * (over + w / 2) ** 2) / (2 * w);
  return t + over / r;
}

export function autoMakeup(p) {
  if (!p.autoGain) return 0;
  const ref = p.autoGain === 1 ? 0 : -12;
  return ref - gainComputer(ref, p.threshold, p.ratio, kneeDb(p));
}

const TB = Math.tanh(0.25);
function saturate(v, kind, amt) {
  if (amt <= 0) return v;
  if (kind === "odd") return (1 - amt) * v + amt * Math.tanh(v);
  return (1 - amt) * v + (amt * (Math.tanh(v + 0.25) - TB)) / (1 - TB * TB);
}

function distort(v, mode) {
  if (mode === "soft") return Math.tanh(v);
  const c = Math.min(1, Math.max(-1, v));
  if (mode === "hard") return 1.5 * c - 0.5 * c ** 3;
  return mode === "clip" ? c : v;
}

// One compressor per playing clip; process() takes any length that is a multiple of BLOCK.
export function createCompressor(raw) {
  const p = normalize(raw);
  const c = CIRCUITS[p.circuit];
  const useRms = c.rms === null ? p.rms : c.rms;
  const w = kneeDb(p);
  const aAtt = coef(p.attack), aRms = coef(RMS_MS), aAvg = coef(AUTO_AVG_MS), aLim = coef(LIMIT_RELEASE_MS);
  const autoK = c.autoK || (p.autoRelease ? 1 : 0);
  const inG = 10 ** (p.inGain / 20), outG = 10 ** (p.outGain / 20);
  const makeup = 10 ** ((p.makeup + autoMakeup(p)) / 20);
  const thr = 10 ** (p.limiterThreshold / 20);
  const la = p.lookahead ? LOOKAHEAD : 0;
  const delay = [new Float64Array(la + BLOCK), new Float64Array(la + BLOCK)]; // raw x, oldest first
  const s = { gs: 0, msq: 0, avg: 0, prevDb: 0, lim: 1, gr: 0 };
  const wet = [new Float64Array(BLOCK), new Float64Array(BLOCK)];

  function controlStep(L, R, o) {
    let pk = 0, m = 0;
    if (p.link === "sum") {
      for (let i = 0; i < BLOCK; i += 1) {
        const v = ((L[o + i] + R[o + i]) / 2) * inG;
        pk = Math.max(pk, Math.abs(v));
        m += v * v;
      }
      m /= BLOCK;
    } else {
      let mL = 0, mR = 0, pL = 0, pR = 0;
      for (let i = 0; i < BLOCK; i += 1) {
        const l = L[o + i] * inG, r = R[o + i] * inG;
        pL = Math.max(pL, Math.abs(l)); pR = Math.max(pR, Math.abs(r));
        mL += l * l; mR += r * r;
      }
      pk = Math.max(pL, pR);
      m = Math.max(mL / BLOCK, mR / BLOCK);
    }
    s.msq = aRms * s.msq + (1 - aRms) * m;
    let level = useRms ? db(Math.sqrt(s.msq)) : db(pk);
    if (c.feedback) level += s.gs;
    const gc = gainComputer(level, p.threshold, p.ratio, w) - level;
    s.avg = aAvg * s.avg + (1 - aAvg) * s.gs;
    const aRel = coef(p.release * (1 + autoK * Math.min(1, -s.avg / 12)));
    const a = gc < s.gs ? aAtt : aRel;
    s.gs = a * s.gs + (1 - a) * gc;
    return s.gs;
  }

  function process(inL, inR, outL, outR) {
    const ins = [inL, inR], outs = [outL, outR];
    for (let o = 0; o < inL.length; o += BLOCK) {
      for (let ch = 0; ch < 2; ch += 1) { // shift the delay line one block and append this block
        delay[ch].copyWithin(0, BLOCK);
        delay[ch].set(ins[ch].subarray(o, o + BLOCK), la);
      }
      const cur = controlStep(inL, inR, o), prev = s.prevDb;
      s.prevDb = cur;
      s.gr = cur;
      let pk = 0;
      for (let ch = 0; ch < 2; ch += 1) {
        for (let i = 0; i < BLOCK; i += 1) {
          const g = 10 ** ((prev + ((cur - prev) * (i + 1)) / BLOCK) / 20);
          const x = delay[ch][i];
          let v = x * inG * g * makeup;
          v = distort(saturate(v, c.kind, c.color), p.distortion);
          v = x * (1 - p.mix) + v * p.mix;
          wet[ch][i] = v;
          pk = Math.max(pk, Math.abs(v));
        }
      }
      let limPrev = s.lim;
      if (p.limiter) {
        const need = Math.min(1, thr / Math.max(pk, FLOOR));
        s.lim = need < s.lim ? need : aLim * s.lim + (1 - aLim) * need;
      } else limPrev = s.lim = 1;
      for (let ch = 0; ch < 2; ch += 1) {
        for (let i = 0; i < BLOCK; i += 1) {
          let v = wet[ch][i];
          if (p.limiter) v = Math.min(thr, Math.max(-thr, v * (limPrev + ((s.lim - limPrev) * (i + 1)) / BLOCK)));
          outs[ch][o + i] = v * outG;
        }
      }
    }
  }
  return { process, params: p, get reduction() { return s.gr; } };
}
