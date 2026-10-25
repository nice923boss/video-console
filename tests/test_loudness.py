"""The preview's loudness meter (MX-01, web/js/lib/loudness.js) against EBU Tech 3341 cases and ffmpeg's ebur128."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from test_audio_export import integrated_lufs
from test_server import needs_ffmpeg
from test_timeline_keys import JS, run_js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# Builds the stereo signal from data.segments ([dBFS, seconds], sine or seeded noise), feeds it in 128-frame
# blocks as the AudioWorklet does, optionally writes it as raw f32le for ffmpeg, and prints the meter's reading.
METER = """
const { rate, freq = 1000, phase = 0, segments, noise = false, out } = data;
const total = segments.reduce((s, [, sec]) => s + Math.round(sec * rate), 0);
const L = new Float32Array(total);
const R = new Float32Array(total);
let seed = 22222;
let lpL = 0;
let lpR = 0; // one-pole low-pass: noise with less energy near Nyquist, like speech and music
const rand = () => { seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5; return (seed >>> 0) / 2147483648 - 1; };
let i = 0;
for (const [db, sec] of segments) {
  const amp = 10 ** (db / 20);
  for (let k = Math.round(sec * rate); k > 0; k -= 1, i += 1) {
    if (noise) { lpL += 0.5 * (rand() - lpL); lpR += 0.5 * (rand() - lpR); L[i] = amp * lpL; R[i] = amp * lpR; }
    else { L[i] = R[i] = amp * Math.sin((2 * Math.PI * freq * i) / rate + (phase * Math.PI) / 180); }
  }
}
const meter = new m.LoudnessMeter(rate);
for (let s = 0; s < total; s += 128) meter.push([L.subarray(s, s + 128), R.subarray(s, s + 128)]);
if (out) {
  const both = new Float32Array(total * 2);
  for (let k = 0; k < total; k += 1) { both[2 * k] = L[k]; both[2 * k + 1] = R[k]; }
  (await import("node:fs")).writeFileSync(out, Buffer.from(both.buffer));
}
const reading = meter.read();
meter.reset();
console.log(JSON.stringify({ ...reading, afterReset: meter.integrated === -Infinity }));
"""


def measure(**data: object) -> dict:
    return run_js(JS / "lib" / "loudness.js", METER, data)


@pytest.mark.parametrize("rate", [48000, 44100])
def test_stereo_sine_at_minus_23(rate: int) -> None:
    """Tech 3341 case 1: a 1 kHz sine at -23 dBFS in both channels reads -23.0 on every meter."""
    got = measure(rate=rate, segments=[[-23, 20]])
    for key in ("momentary", "shortTerm", "integrated"):
        assert got[key] == pytest.approx(-23.0, abs=0.1), key
    assert got["truePeak"] == pytest.approx(-23.0, abs=0.1)
    assert got["afterReset"] is True


def test_gating_ignores_quiet_and_silent_parts() -> None:
    """Tech 3341 case 4: -72 / -36 / -23 / -36 / -72 dBFS; the absolute and relative gates leave -23."""
    got = measure(rate=48000, segments=[[-72, 10], [-36, 10], [-23, 60], [-36, 10], [-72, 10]])
    assert got["integrated"] == pytest.approx(-23.0, abs=0.1)


def test_true_peak_between_samples() -> None:
    """Tech 3341 case 18: a 12 kHz sine at 0.5 FS whose samples never land on the crest (they peak at -9 dBFS)."""
    got = measure(rate=48000, freq=12000, phase=45, segments=[[-6.0206, 2]])
    assert -6.4 <= got["truePeak"] <= -5.8


@needs_ffmpeg
def test_noise_matches_ffmpeg(tmp_path: Path) -> None:
    """Full-band white noise reads up to 1 dB lower true peak than ffmpeg: BS.1770's 48-tap interpolator rolls off
    near Nyquist where ffmpeg's longer resampler does not (GAPS)."""
    raw = tmp_path / "noise.f32"
    got = measure(rate=48000, noise=True, segments=[[-20, 4], [-38, 4], [-14, 4], [-80, 2]], out=str(raw))
    lufs, peak = integrated_lufs(raw, "-f", "f32le", "-ar", "48000", "-ac", "2")
    assert got["integrated"] == pytest.approx(lufs, abs=0.2)
    assert got["truePeak"] == pytest.approx(peak, abs=0.5)
