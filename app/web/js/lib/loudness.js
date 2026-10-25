// Loudness as ITU-R BS.1770-4 / EBU R128 measure it (MX-01): K-weighted momentary (400 ms), short-term (3 s)
// and gated integrated loudness in LUFS, and the true peak in dBTP from 4x oversampling.
// Pure, so the AudioWorklet (loudness-worklet.js) runs it on the preview mix and node tests check it
// against ffmpeg's ebur128 filter.
const OFFSET = -0.691;
const ABS_GATE = -70;
const REL_GATE = -10;
const SHORT = 30; // 100 ms segments in the 3 s short-term window
const BLOCK = 4; // segments in a 400 ms gating block (75% overlap: one block per segment)
const PHASES = 4;
const TAPS = 12; // per phase: the 48-tap interpolator BS.1770 Annex 2 describes

export const toLufs = (meanSquare) => (meanSquare > 0 ? OFFSET + 10 * Math.log10(meanSquare) : -Infinity);
const toDb = (v) => (v > 0 ? 20 * Math.log10(v) : -Infinity);

// The two K-weighting biquads (high shelf, then high pass) for any sample rate, as libebur128 derives them.
function kFilters(rate) {
  let f0 = 1681.974450955533;
  const G = 3.999843853973347;
  let Q = 0.7071752369554196;
  let K = Math.tan((Math.PI * f0) / rate);
  const Vh = 10 ** (G / 20);
  const Vb = Vh ** 0.4996667741545416;
  let a0 = 1 + K / Q + K * K;
  const shelf = {
    b: [(Vh + (Vb * K) / Q + K * K) / a0, (2 * (K * K - Vh)) / a0, (Vh - (Vb * K) / Q + K * K) / a0],
    a: [(2 * (K * K - 1)) / a0, (1 - K / Q + K * K) / a0],
  };
  f0 = 38.13547087602444;
  Q = 0.5003270373238773;
  K = Math.tan((Math.PI * f0) / rate);
  a0 = 1 + K / Q + K * K;
  const highPass = { b: [1, -2, 1], a: [(2 * (K * K - 1)) / a0, (1 - K / Q + K * K) / a0] };
  return [shelf, highPass];
}

// Hann-windowed sinc for the three in-between phases, each normalized to unity gain; phase 0 is the sample itself.
function interpolator() {
  const half = TAPS / 2;
  return [1, 2, 3].map((p) => {
    const h = new Float64Array(TAPS);
    let sum = 0;
    for (let k = 0; k < TAPS; k += 1) {
      const d = half - k - p / PHASES; // distance from the in-between point to sample n-k
      const sinc = d === 0 ? 1 : Math.sin(Math.PI * d) / (Math.PI * d);
      const w = Math.abs(d) < half ? 0.5 * (1 + Math.cos((Math.PI * d) / half)) : 0;
      h[k] = sinc * w;
      sum += h[k];
    }
    return h.map((v) => v / sum);
  });
}

export class LoudnessMeter {
  constructor(rate) {
    this.rate = rate;
    this.filters = kFilters(rate);
    this.phases = interpolator();
    this.segLen = Math.round(rate / 10);
    this.reset();
  }

  reset() {
    // Per channel: biquad state [x1, x2, y1, y2] for each filter, and a doubled history for the interpolator.
    this.state = [0, 1].map(() => this.filters.map(() => new Float64Array(4)));
    this.hist = [0, 1].map(() => new Float64Array(TAPS * 2));
    this.histAt = 0;
    this.segs = new Float64Array(SHORT); // sum of squares of each recent segment, both channels added
    this.segAt = 0;
    this.segCount = 0;
    this.acc = 0;
    this.accN = 0;
    this.blocks = []; // mean squares of the gating blocks above the absolute gate
    this.cached = null;
    this.peak = 0;
  }

  // channels: one or two Float32Arrays of equal length (mono counts once, as BS.1770 weighs it).
  push(channels) {
    const n = channels[0].length;
    const chans = channels.length > 1 ? 2 : 1;
    for (let i = 0; i < n; i += 1) {
      let sq = 0;
      for (let c = 0; c < chans; c += 1) {
        const x = channels[c][i];
        this.truePeak(c, x);
        let v = x;
        for (let f = 0; f < 2; f += 1) {
          const { b, a } = this.filters[f];
          const s = this.state[c][f];
          const y = b[0] * v + b[1] * s[0] + b[2] * s[1] - a[0] * s[2] - a[1] * s[3];
          s[1] = s[0];
          s[0] = v;
          s[3] = s[2];
          s[2] = y;
          v = y;
        }
        sq += v * v;
      }
      this.histAt = (this.histAt + 1) % TAPS;
      this.acc += sq;
      this.accN += 1;
      if (this.accN === this.segLen) this.endSegment();
    }
  }

  truePeak(c, x) {
    const h = this.hist[c];
    const at = this.histAt; // slot of this sample; h[at + TAPS - k] is the sample k steps back
    h[at] = x;
    h[at + TAPS] = x;
    let peak = Math.abs(x);
    for (const ph of this.phases) {
      let y = 0;
      for (let k = 0; k < TAPS; k += 1) y += ph[k] * h[at + TAPS - k];
      if (Math.abs(y) > peak) peak = Math.abs(y);
    }
    if (peak > this.peak) this.peak = peak;
  }

  endSegment() {
    this.segs[this.segAt] = this.acc;
    this.segAt = (this.segAt + 1) % SHORT;
    this.segCount += 1;
    this.acc = 0;
    this.accN = 0;
    if (this.segCount < BLOCK) return;
    const ms = this.windowMean(BLOCK);
    if (toLufs(ms) > ABS_GATE) {
      this.blocks.push(ms);
      this.cached = null;
    }
  }

  // Mean square over the last `count` segments; segments before the first count as silence.
  windowMean(count) {
    let sum = 0;
    for (let i = 1; i <= count; i += 1) sum += this.segs[(this.segAt - i + SHORT) % SHORT];
    return sum / (count * this.segLen);
  }

  get momentary() {
    return toLufs(this.windowMean(BLOCK));
  }

  get shortTerm() {
    return toLufs(this.windowMean(SHORT));
  }

  // Blocks above the absolute gate, then those within 10 LU of their mean.
  get integrated() {
    if (this.cached !== null) return this.cached;
    const { blocks } = this;
    if (!blocks.length) return (this.cached = -Infinity);
    const gate = toLufs(blocks.reduce((s, v) => s + v, 0) / blocks.length) + REL_GATE;
    let sum = 0;
    let n = 0;
    for (const v of blocks) {
      if (toLufs(v) > gate) {
        sum += v;
        n += 1;
      }
    }
    return (this.cached = n ? toLufs(sum / n) : -Infinity);
  }

  get truePeakDb() {
    return toDb(this.peak);
  }

  read() {
    return { momentary: this.momentary, shortTerm: this.shortTerm, integrated: this.integrated, truePeak: this.truePeakDb };
  }
}
