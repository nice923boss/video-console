// Mirror of the remix half of vc/voiceclean.py: what an audio clip's 降噪 / 去回音 settings ask of its stems
// (the source X, the denoised D and D with the echo taken out V). The preview (audio-mix.js) remixes them live with
// these gains, so moving Strength, the bands or Bass / Body / Tone needs no new pass; tests/test_voiceclean_js.py
// checks the two sides agree.
export const DENOISE_FLOOR_DB = 30;
export const ECHO_FLOOR_DB = 25;
export const LOW_HZ = 250;
export const HIGH_HZ = 4000;
export const BUTTERWORTH_Q = 1 / Math.sqrt(2);
export const BASS_HZ = 150;
export const BASS_DB = 24;
export const BODY_HZ = 280;
export const BODY_Q = 0.7;
export const BODY_DB = 6;
export const TONE_HZ = 5000;
export const TONE_DB = 6;
export const MIN_SAMPLE = 0.25;
export const MAX_SAMPLE = 10;

export const DEFAULT_DENOISE = { on: true, strength: 0.6, low: 1, mid: 1, high: 1, sample: null };
export const DEFAULT_DEREVERB = { on: true, strength: 0.6, release: 0.3, dryness: 0.5, bass: 0, body: 0, tone: 0 };

// Share of the removed sound put back so the effect takes at most strength * floorDb off.
export function keep(strength, floorDb) {
  const fl = 10 ** (-floorDb / 20);
  return (10 ** ((-floorDb * strength) / 20) - fl) / (1 - fl);
}

// Channels per stem: 1 when the clip plays one channel of its file, else 2.
export const stemChannels = (item) => (item.channel ? 1 : 2);

// What the pass over the clip depends on; null when it would only give back the source as it is.
export function passParams(item) {
  const dn = item.denoise || {};
  const dr = item.dereverb || {};
  const sample = dn.on ? dn.sample || null : null;
  const echo = dr.on ? { dryness: dr.dryness, release: dr.release } : null;
  const channel = item.channel || null;
  if (!sample && !echo && !channel) return null;
  return { sample, echo, channel };
}

// gr for the echo, gl / gm / gh for the noise per band (1 puts everything back), and eq, the echo remover's
// [kind, Hz, dB, Q] filters that change anything.
export function mixPlan(item) {
  const dn = item.denoise || {};
  const dr = item.dereverb || {};
  const plan = { gr: 1, gl: 1, gm: 1, gh: 1, eq: [] };
  if (dn.on && dn.sample) {
    plan.gl = keep(dn.strength * dn.low, DENOISE_FLOOR_DB);
    plan.gm = keep(dn.strength * dn.mid, DENOISE_FLOOR_DB);
    plan.gh = keep(dn.strength * dn.high, DENOISE_FLOOR_DB);
  }
  if (dr.on) {
    plan.gr = keep(dr.strength, ECHO_FLOOR_DB);
    plan.eq = [
      ["lowshelf", BASS_HZ, -BASS_DB * dr.bass, BUTTERWORTH_Q],
      ["peaking", BODY_HZ, BODY_DB * dr.body, BODY_Q],
      ["highshelf", TONE_HZ, TONE_DB * dr.tone, BUTTERWORTH_Q],
    ].filter((f) => Math.abs(f[2]) > 1e-6);
  }
  return plan;
}
