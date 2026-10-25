"""Voice cleanup for audio clips: noise reduction learned from a sample of the noise (after CrumplePop
AudioDenoise 2) and a classic, non-AI echo remover (after the 2019 EchoRemover).

One pass per clip writes three stems into one WAV: X (the source), D (X with the sampled noise taken out) and
V (D with the late reverb taken out), each stereo (6 channels), or mono (3 channels) when the clip plays just
one channel of its file. Every other knob only remixes those stems, by the same formulas here (mix_plan, used by
export.py) and in web/js/lib/voice-mix.js, so preview and export agree and the sliders answer at once. Only a
new noise sample, Release, Dryness, the channel or switching an effect on or off needs a new pass.

  noise:  a decision-directed Wiener gain against the noise spectrum averaged over the sample
  echo:   Lebart's late-reverb estimate (the power 50 ms earlier, decayed at an assumed RT60), subtracted
          from the power, with the gain falling no faster than Release allows

Both gains are worked out on the mono power and applied to both channels, so the stereo image stays put. A
clip set to one channel (a lavalier on the left, a second mic on the right) decodes only that channel, so the
pass never sees the others.

The remix, with N = X - D the noise taken out and LP1/LP2 Butterworth low-passes at the band edges:

  Y = V + gr*(D - V) + gh*N + (gm - gh)*LP2(N) + (gl - gm)*LP1(N)

then the echo remover's Bass, Body and Tone filters. Strength is how far down the effect may take a sound
(DENOISE_FLOOR_DB or ECHO_FLOOR_DB at 100%), and the Low/Mid/High sliders scale it per band.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import subprocess
import threading
import wave
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterator

from .media import NO_WINDOW, Media
from .sequence import audio_media
from .util import AppError

RATE = 48000
N_FFT = 2048
HOP = N_FFT // 4  # sqrt-Hann in and out at 75% overlap adds up to a constant 2
DENOISE_FLOOR_DB = 30.0  # the most the noise reduction takes off (Strength 100%)
ECHO_FLOOR_DB = 25.0  # the most the echo remover takes off (Strength 100%)
SMOOTH_DD = 0.98  # decision-directed weight of the previous frame's estimate
LATE_MS = 50.0  # reverb later than this after the direct sound counts as echo
SMOOTH_POWER = 0.5  # light smoothing of the power the reverb estimate works from
EPS = 1e-12

DSP_VERSION = 1  # part of every stems file's name: a change to the pass above must bump it
CLEAN_DIR = "media/clean"
PAD = 2.0  # seconds of source rendered beyond each end of the clip, so a small trim needs no new pass
MIN_SAMPLE = 0.25
MAX_SAMPLE = 10.0
BLOCK = RATE * 4  # samples decoded per step of a pass
OVERVIEW_RATE = 16000  # the channel overview's levels need no more
OVERVIEW_BUCKETS = 200
OVERVIEW_FLOOR_DB = -60.0  # the bottom of the overview's level bars

LOW_HZ = 250.0  # Low | Mid edge of the noise reduction's bands
HIGH_HZ = 4000.0  # Mid | High edge
BUTTERWORTH_Q = 1 / math.sqrt(2)
BASS_HZ, BASS_DB = 150.0, 24.0  # low shelf, 0 to -24 dB
BODY_HZ, BODY_Q, BODY_DB = 280.0, 0.7, 6.0  # peak, -6 to +6 dB
TONE_HZ, TONE_DB = 5000.0, 6.0  # high shelf, -6 to +6 dB

DEFAULT_DENOISE = {"on": True, "strength": 0.6, "low": 1.0, "mid": 1.0, "high": 1.0, "sample": None}
DEFAULT_DEREVERB = {"on": True, "strength": 0.6, "release": 0.3, "dryness": 0.5, "bass": 0.0, "body": 0.0,
                    "tone": 0.0}


def _np() -> Any:
    try:
        import numpy  # only the voice cleanup needs it
    except ImportError:
        raise AppError("降噪與去回音需要 numpy：請執行「安裝相依套件.bat」，或 pip install numpy", 503) from None
    return numpy


def echo_rt60(dryness: float) -> float:
    """Dryness 0..1 as the reverb time assumed for the room: a longer room means more is taken off."""
    return 0.5 + 0.5 * dryness


def echo_oversub(dryness: float) -> float:
    """Dryness 0..1 as how many times the estimated echo is taken off. Once is not enough: the tail of a room
    is noise-like, and removing its mean power from each bin only takes about 4 dB off it."""
    return 1.0 + 3.0 * dryness


def release_seconds(release: float) -> float:
    """Release 0..1 as how slowly the echo gain may fall, 10 ms to 400 ms."""
    return 0.01 * 40 ** release


class Pass:
    """Turns blocks of the source (1 or 2 channels) into rows of [X, D, V], carrying the STFT overlap and the
    gain recursions from block to block, so a long clip never has to be in memory at once."""

    def __init__(self, noise: Any = None, echo: dict | None = None, channels: int = 2):
        np = self.np = _np()
        self.win = np.sqrt(np.hanning(N_FFT + 1)[:-1]).astype(np.float32)  # periodic
        self.noise = noise
        self.echo = echo
        self.ch = channels
        bins = N_FFT // 2 + 1
        self.dd = np.ones(bins)  # previous frame's G^2 * gamma for the decision-directed estimate
        self.floor_n = 10 ** (-DENOISE_FLOOR_DB / 20)
        if echo:
            delay = max(1, round(LATE_MS / 1000 * RATE / HOP))
            self.decay = math.exp(-2 * (3 * math.log(10) / echo_rt60(echo["dryness"])) * delay * HOP / RATE)
            self.rel = math.exp(-HOP / (RATE * release_seconds(echo["release"])))
            self.oversub = echo_oversub(echo["dryness"])
            self.floor_r = 10 ** (-ECHO_FLOOR_DB / 20)
            self.power = np.zeros(bins)
            self.history: deque = deque([np.zeros(bins)] * delay, maxlen=delay)
            self.gr = np.ones(bins)
        self.inbuf = np.zeros((N_FFT - HOP, channels), np.float32)  # leading zeros: sample 0 gets all four frames
        self.tail = np.zeros((N_FFT - HOP, 2 * channels), np.float32)
        self.skip = N_FFT - HOP

    def _gains(self, power: Any) -> tuple[Any, Any]:
        np = self.np
        gn = np.ones_like(power)
        gr = np.ones_like(power)
        for f, p in enumerate(power):
            if self.noise is not None:
                gamma = p / self.noise
                xi = SMOOTH_DD * self.dd + (1 - SMOOTH_DD) * np.maximum(gamma - 1, 0)
                g = np.maximum(xi / (1 + xi), self.floor_n)
                self.dd = g * g * gamma
                gn[f] = g
                p = p * g * g
            if self.echo:
                late = self.decay * self.history[0]
                self.power = SMOOTH_POWER * self.power + (1 - SMOOTH_POWER) * p
                self.history.append(self.power)
                g = np.sqrt(np.maximum(1 - self.oversub * late / (self.power + EPS), self.floor_r ** 2))
                g = np.where(g < self.gr, self.rel * self.gr + (1 - self.rel) * g, g)
                self.gr = g
                gr[f] = g
        return gn, gr

    def feed(self, x: Any) -> Any:
        """float32 samples in, the finished [X, D, V] rows out (fewer at first: the STFT lags)."""
        np = self.np
        c = self.ch
        if self.noise is None and not self.echo:  # only picking a channel: nothing to take out
            return np.concatenate([x, x, x], axis=1)
        data = np.concatenate([self.inbuf, x])
        nf = (len(data) - N_FFT) // HOP + 1
        if nf <= 0:
            self.inbuf = data
            return np.zeros((0, 3 * c), np.float32)
        idx = np.arange(N_FFT)[None, :] + HOP * np.arange(nf)[:, None]
        spec = np.fft.rfft(data[idx] * self.win[None, :, None], axis=1)  # frames, bins, channels
        gn, gr = self._gains((spec.real ** 2 + spec.imag ** 2).mean(axis=2))
        d = spec * gn[:, :, None]
        frames = np.fft.irfft(np.concatenate([d, d * gr[:, :, None]], axis=2), n=N_FFT, axis=1)
        frames = (frames * (self.win[None, :, None] * 0.5)).astype(np.float32).reshape(nf, 4, HOP, 2 * c)
        out = np.zeros((nf + 3, HOP, 2 * c), np.float32)
        out[:3] += self.tail.reshape(3, HOP, 2 * c)
        for j in range(4):
            out[j:j + nf] += frames[:, j]
        out = out.reshape(-1, 2 * c)
        emit = nf * HOP
        self.tail = out[emit:]
        rows = np.concatenate([data[:emit], out[:emit]], axis=1)
        self.inbuf = data[emit:]
        if self.skip:
            cut = min(self.skip, emit)
            rows = rows[cut:]
            self.skip -= cut
        return rows

    def flush(self) -> Any:
        if self.noise is None and not self.echo:
            return self.np.zeros((0, 3 * self.ch), self.np.float32)
        return self.feed(self.np.zeros((N_FFT, self.ch), self.np.float32))


def noise_profile(x: Any) -> Any:
    """Mean power per bin of a sample of the noise (1 or 2 channels)."""
    np = _np()
    if len(x) < N_FFT:
        raise AppError("雜訊樣本太短，至少要 0.25 秒")
    win = np.sqrt(np.hanning(N_FFT + 1)[:-1])
    nf = (len(x) - N_FFT) // HOP + 1
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(nf)[:, None]
    spec = np.fft.rfft(x[idx] * win[None, :, None], axis=1)
    return (spec.real ** 2 + spec.imag ** 2).mean(axis=(0, 2)) + EPS


# ------------------------------------------------------------ settings on an audio clip

def _num(v: Any, lo: float, hi: float, default: float, message: str) -> float:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise AppError(message)
    return round(max(lo, min(hi, float(v))), 4)


def clean_denoise(v: Any) -> dict:
    """An audio clip's noise reduction: missing fields take their default, numbers are clamped. sample is the
    stretch of the clip's source file (seconds) that holds only the noise, None until one is taken."""
    message = "降噪設定格式不正確"
    if not isinstance(v, dict) or set(v) - set(DEFAULT_DENOISE):
        raise AppError(message)
    out: dict = {"on": bool(v.get("on", True))}
    for k in ("strength", "low", "mid", "high"):
        out[k] = _num(v.get(k), 0.0, 1.0, DEFAULT_DENOISE[k], message)
    sample = v.get("sample")
    if sample is not None:
        if not isinstance(sample, dict) or set(sample) != {"from", "to"}:
            raise AppError(message)
        lo = _num(sample["from"], 0.0, 36000.0, 0.0, message)
        hi = _num(sample["to"], 0.0, 36000.0, 0.0, message)
        if hi - lo < MIN_SAMPLE:
            raise AppError("雜訊樣本太短，至少要 0.25 秒")
        sample = {"from": lo, "to": round(min(hi, lo + MAX_SAMPLE), 4)}
    out["sample"] = sample
    return out


def clean_dereverb(v: Any) -> dict:
    """An audio clip's echo remover: missing fields take their default, numbers are clamped."""
    message = "去回音設定格式不正確"
    if not isinstance(v, dict) or set(v) - set(DEFAULT_DEREVERB):
        raise AppError(message)
    out: dict = {"on": bool(v.get("on", True))}
    for k in ("strength", "release", "dryness", "bass"):
        out[k] = _num(v.get(k), 0.0, 1.0, DEFAULT_DEREVERB[k], message)
    for k in ("body", "tone"):
        out[k] = _num(v.get(k), -1.0, 1.0, DEFAULT_DEREVERB[k], message)
    return out


def clean_channel(v: Any) -> dict | None:
    """The one channel of its file an audio clip plays, {"stream": which audio stream of the file, "index": which
    channel of it}, or None to play the first stream mixed to stereo."""
    if v is None:
        return None
    if (not isinstance(v, dict) or set(v) != {"stream", "index"}
            or any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n < 64 for n in v.values())):
        raise AppError("聲道設定格式不正確")
    return {"stream": v["stream"], "index": v["index"]}


def stem_channels(item: dict) -> int:
    """Channels per stem: 1 when the clip plays one channel of its file, else 2."""
    return 1 if item.get("channel") else 2


def pass_params(item: dict) -> dict | None:
    """What the pass over an audio clip depends on; None when it would only give back the source as it is."""
    dn = item.get("denoise") or {}
    dr = item.get("dereverb") or {}
    sample = dn.get("sample") if dn.get("on") else None
    echo = {"dryness": dr["dryness"], "release": dr["release"]} if dr.get("on") else None
    channel = item.get("channel")
    if not sample and not echo and not channel:
        return None
    return {"sample": sample, "echo": echo, "channel": channel}


# ------------------------------------------------------------ the remix (mirrored in web/js/lib/voice-mix.js)

def keep(strength: float, floor_db: float) -> float:
    """Share of the removed sound put back so the effect takes at most strength * floor_db off: the pass already
    took floor_db off where it acted fully, so a share k of the difference leaves fl + k * (1 - fl)."""
    fl = 10 ** (-floor_db / 20)
    return (10 ** (-floor_db * strength / 20) - fl) / (1 - fl)


def biquad(kind: str, freq: float, gain_db: float = 0.0, q: float = BUTTERWORTH_Q,
           rate: float = RATE) -> tuple[float, float, float, float, float]:
    """(b0, b1, b2, a1, a2) over a0, by the Web Audio BiquadFilterNode formulas (shelves at slope 1), so the
    preview's filters and the export's match."""
    w0 = 2 * math.pi * freq / rate
    cw, sw = math.cos(w0), math.sin(w0)
    a = 10 ** (gain_db / 40)
    if kind == "lowpass":
        alpha = sw / (2 * q)
        b = ((1 - cw) / 2, 1 - cw, (1 - cw) / 2)
        den = (1 + alpha, -2 * cw, 1 - alpha)
    elif kind == "peaking":
        alpha = sw / (2 * q)
        b = (1 + alpha * a, -2 * cw, 1 - alpha * a)
        den = (1 + alpha / a, -2 * cw, 1 - alpha / a)
    else:
        k = 2 * math.sqrt(a) * sw / 2 * math.sqrt(2)
        if kind == "lowshelf":
            b = (a * ((a + 1) - (a - 1) * cw + k), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - k))
            den = ((a + 1) + (a - 1) * cw + k, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - k)
        elif kind == "highshelf":
            b = (a * ((a + 1) + (a - 1) * cw + k), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - k))
            den = ((a + 1) - (a - 1) * cw + k, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - k)
        else:
            raise ValueError(kind)
    return b[0] / den[0], b[1] / den[0], b[2] / den[0], den[1] / den[0], den[2] / den[0]


def mix_plan(item: dict) -> dict:
    """The remix gains of an audio clip: gr for the echo, gl/gm/gh for the noise per band (1 puts everything back),
    and eq, the echo remover's (kind, Hz, dB, Q) filters that change anything."""
    dn = item.get("denoise") or {}
    dr = item.get("dereverb") or {}
    plan: dict = {"gr": 1.0, "gl": 1.0, "gm": 1.0, "gh": 1.0, "eq": []}
    if dn.get("on") and dn.get("sample"):
        for band in ("low", "mid", "high"):
            plan["g" + band[0]] = keep(dn["strength"] * dn[band], DENOISE_FLOOR_DB)
    if dr.get("on"):
        plan["gr"] = keep(dr["strength"], ECHO_FLOOR_DB)
        eq = [("lowshelf", BASS_HZ, -BASS_DB * dr["bass"], BUTTERWORTH_Q),
              ("peaking", BODY_HZ, BODY_DB * dr["body"], BODY_Q),
              ("highshelf", TONE_HZ, TONE_DB * dr["tone"], BUTTERWORTH_Q)]
        plan["eq"] = [f for f in eq if abs(f[2]) > 1e-6]
    return plan


def _terms(pairs: list[tuple[float, int]]) -> str:
    """Input channels weighted, for a pan channel: '0.25*c0-0.25*c2+c4'."""
    out = ""
    for g, ch in pairs:
        if abs(g) < 1e-6:
            continue
        sign = "-" if g < 0 else ("+" if out else "")
        out += f"{sign}{abs(g):.6f}*c{ch}"
    return out or "0*c0"


def _biquad_filter(coef: tuple[float, ...], gain: float = 1.0) -> str:
    b0, b1, b2, a1, a2 = coef
    return (f"biquad=b0={b0 * gain:.10g}:b1={b1 * gain:.10g}:b2={b2 * gain:.10g}"
            f":a0=1:a1={a1:.10g}:a2={a2:.10g}")


def mix_lines(item: dict, src: str, out: str) -> list[str]:
    """ffmpeg filtergraph lines turning the stems at [src] into the clip's stereo sound at [out] (one picked
    channel plays in the middle)."""
    p = mix_plan(item)
    gr, gl, gm, gh = p["gr"], p["gl"], p["gm"], p["gh"]
    c = stem_channels(item)

    def at(stem: int, o: int) -> int:  # the stems file's channel feeding output channel o
        return stem * c + (o if c == 2 else 0)

    base = "pan=stereo|" + "|".join(
        f"c{o}={_terms([(gh, at(0, o)), (gr - gh, at(1, o)), (1 - gr, at(2, o))])}" for o in (0, 1))
    noise = "pan=stereo|" + "|".join(f"c{o}=c{at(0, o)}-c{at(1, o)}" for o in (0, 1))
    bands = [(gl - gm, LOW_HZ), (gm - gh, HIGH_HZ)]
    bands = [(g, hz) for g, hz in bands if abs(g) > 1e-6]
    eq = "".join(f",{_biquad_filter(biquad(kind, hz, db, q))}" for kind, hz, db, q in p["eq"])
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000"  # keeps every channel
    if not bands:
        return [f"[{src}]{fmt},{base}{eq}[{out}]"]
    n = len(bands) + 1
    lines = [f"[{src}]{fmt},asplit={n}" + "".join(f"[{out}s{i}]" for i in range(n)),
             f"[{out}s0]{base}[{out}m0]"]
    for i, (g, hz) in enumerate(bands, 1):
        lines.append(f"[{out}s{i}]{noise},{_biquad_filter(biquad('lowpass', hz), g)}[{out}m{i}]")
    sums = "|".join(f"c{c}=" + "+".join(f"c{2 * i + c}" for i in range(n)) for c in (0, 1))
    lines.append("".join(f"[{out}m{i}]" for i in range(n)) + f"amerge=inputs={n},pan=stereo|{sums}{eq}[{out}]")
    return lines


# ------------------------------------------------------------ stems files

def source_span(item: dict, src: dict) -> tuple[float, float, float]:
    """(in, out, duration) of the clip in its source file, out clamped to the file."""
    dur = float(src.get("duration") or 0.0)
    a_in = float(item.get("in") or 0.0)
    a_out = item.get("out")
    a_out = dur if a_out is None else (min(float(a_out), dur) if dur else float(a_out))
    return a_in, a_out, dur


def _stems_hash(path: Path, params: dict) -> str:
    st = path.stat()
    raw = json.dumps([DSP_VERSION, path.name, st.st_size, st.st_mtime_ns, params], sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def stems_for(project: dict, project_dir: Path, item: dict) -> dict | None:
    """The finished stems covering this clip as it is now: {"file", "from", "to"} (source seconds), or None."""
    params = pass_params(item)
    src = audio_media(project, item)
    if not params or not src or not (project_dir / src["file"]).is_file():
        return None
    a_in, a_out, _ = source_span(item, src)
    prefix = f"{item['id']}-{_stems_hash(project_dir / src['file'], params)}-"
    for path in (project_dir / CLEAN_DIR).glob(f"{prefix}*.wav"):
        try:
            lo, hi = (int(v) / 1000 for v in path.stem[len(prefix):].split("-"))
        except ValueError:
            continue
        if lo <= a_in + 0.001 and hi >= a_out - 0.001:
            return {"file": f"{CLEAN_DIR}/{path.name}", "from": lo, "to": hi}
    return None


_latest: dict[str, int] = {}
_latest_lock = threading.Lock()
_counter = itertools.count(1)


def claim(key: str) -> Callable[[], bool]:
    """Marks a new request for `key`; the check returned turns true once a newer one arrives, so a pass the
    user has already moved on from stops instead of finishing."""
    with _latest_lock:
        n = next(_counter)
        _latest[key] = n
    return lambda: _latest.get(key) != n


def _wav_channels(stream: Any) -> int:
    """Reads the header of the WAV ffmpeg writes to a pipe, up to its samples; the channel count, 0 if none came."""
    if stream.read(12)[8:12] != b"WAVE":
        return 0
    channels = 0
    while True:
        head = stream.read(8)
        if len(head) < 8:
            return 0
        if head[:4] == b"data":
            return channels
        size = int.from_bytes(head[4:], "little")
        body = stream.read(size + size % 2)
        if head[:4] == b"fmt ":
            channels = int.from_bytes(body[2:4], "little")


def _pcm(media: Media, path: Path, start: float, length: float, stream: int, *, rate: int = RATE,
         stereo: bool = False, index: int | None = None,
         cancelled: Callable[[], bool] | None = None) -> Iterator[Any]:
    """float32 blocks of [start, start + length] of one audio stream of the file: all its channels, the stream
    mixed to stereo, or channel `index` alone."""
    np = _np()
    cmd = [media.require(), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}",
           "-t", f"{length:.3f}", "-i", str(path), "-map", f"0:a:{stream}", *(["-ac", "2"] if stereo else []),
           "-ar", str(rate), "-c:a", "pcm_f32le", "-f", "wav", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    assert proc.stdout is not None and proc.stderr is not None
    err: list[bytes] = []
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    reader.start()
    try:
        c = _wav_channels(proc.stdout)
        if index is not None and c and index >= c:
            raise AppError(f"原始檔的音軌 {stream + 1} 只有 {c} 個聲道，沒有聲道 {index + 1}")
        while c:
            if cancelled and cancelled():
                raise AppError("已取消", 499)
            raw = proc.stdout.read(BLOCK * 4 * c)
            if not raw:
                break
            x = np.frombuffer(raw[:len(raw) // (4 * c) * (4 * c)], np.float32).reshape(-1, c)
            yield x if index is None else x[:, index:index + 1]
        proc.wait()
        reader.join(timeout=2)
        if proc.returncode != 0 or not c:
            raise media._error(b"".join(err).decode("utf-8", "replace"))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _source(media: Media, path: Path, start: float, length: float, channel: dict | None,
            cancelled: Callable[[], bool] | None = None) -> Iterator[Any]:
    """The clip's sound as the pass takes it: the picked channel alone, or the first audio stream (the one the
    export plays) as stereo."""
    if channel:
        return _pcm(media, path, start, length, channel["stream"], index=channel["index"], cancelled=cancelled)
    return _pcm(media, path, start, length, 0, stereo=True, cancelled=cancelled)


def _sample(media: Media, path: Path, sample: dict, channel: dict | None) -> Any:
    np = _np()
    blocks = list(_source(media, path, sample["from"], sample["to"] - sample["from"], channel))
    return noise_profile(np.concatenate(blocks) if blocks else np.zeros((0, 1), np.float32))


def _run_pass(media: Media, path: Path, lo: float, hi: float, noise: Any, echo: dict | None,
              channel: dict | None, out: Path, cancelled: Callable[[], bool] | None) -> None:
    """Decode [lo, hi] of the source, run it through a Pass and write the stems as a 16-bit WAV at out."""
    np = _np()
    c = 1 if channel else 2
    dsp = Pass(noise=noise, echo=echo, channels=c)
    total = 0
    written = 0
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(3 * c)
        wav.setsampwidth(2)
        wav.setframerate(RATE)

        def put(rows: Any) -> None:
            nonlocal written
            rows = rows[:max(0, total - written)]
            wav.writeframes((np.clip(rows, -1.0, 1.0) * 32767).astype("<i2").tobytes())
            written += len(rows)

        for x in _source(media, path, lo, hi - lo, channel, cancelled):
            total += len(x)
            put(dsp.feed(x))
        put(dsp.flush())


def ensure_stems(media: Media, project: dict, project_dir: Path, item: dict,
                 cancelled: Callable[[], bool] | None = None) -> dict | None:
    """The stems for an audio clip as it is now, running the pass when no finished file covers it. Returns
    stems_for's {"file", "from", "to"}, or None when the clip has no effect that needs one."""
    params = pass_params(item)
    if not params:
        return None
    found = stems_for(project, project_dir, item)
    if found:
        return found
    src = audio_media(project, item)
    if not src or not (project_dir / src["file"]).is_file():
        raise AppError("找不到這段音訊的原始檔", 404)
    path = project_dir / src["file"]
    a_in, a_out, dur = source_span(item, src)
    if a_out - a_in <= 0:
        raise AppError("這段音訊沒有長度")
    lo = max(0.0, a_in - PAD)
    hi = a_out + PAD if not dur else min(dur, a_out + PAD)
    lo_ms, hi_ms = math.floor(lo * 1000), math.ceil(hi * 1000)
    media.require()
    channel = params["channel"]
    noise = _sample(media, path, params["sample"], channel) if params["sample"] else None
    folder = project_dir / CLEAN_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{item['id']}-{_stems_hash(path, params)}-{lo_ms}-{hi_ms}"
    part = folder / f".{stem}.{threading.get_ident()}.part"
    try:
        _run_pass(media, path, lo_ms / 1000, hi_ms / 1000, noise, params["echo"], channel, part, cancelled)
        os.replace(part, folder / f"{stem}.wav")
    finally:
        part.unlink(missing_ok=True)
    for old in folder.glob(f"{item['id']}-*.wav"):
        if old.stem != stem:
            try:
                old.unlink()
            except OSError:  # still open in an export or the preview; the next pass tries again
                pass
    return {"file": f"{CLEAN_DIR}/{stem}.wav", "from": lo_ms / 1000, "to": hi_ms / 1000}


# ------------------------------------------------------------ the channels of a clip's file

def _audio_streams(media: Media, path: Path) -> int:
    """How many audio streams ffmpeg lists for the file."""
    proc = subprocess.run([media.require(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True,
                          check=False, timeout=60, creationflags=NO_WINDOW)
    text = proc.stderr.decode("utf-8", "replace")
    return sum(1 for line in text.splitlines() if "Stream #" in line and "Audio:" in line)


def channel_levels(media: Media, project: dict, project_dir: Path, item: dict,
                   cancelled: Callable[[], bool] | None = None) -> list[dict]:
    """Every channel of every audio stream of an audio clip's file, over the clip: [{"stream", "channels":
    [{"peaks": OVERVIEW_BUCKETS bars 0..100 over OVERVIEW_FLOOR_DB..0 dBFS, "peakDb", "rmsDb"}]}], with the dB
    None for a channel that is silent throughout."""
    np = _np()
    src = audio_media(project, item)
    if not src or not (project_dir / src["file"]).is_file():
        raise AppError("找不到這段音訊的原始檔", 404)
    path = project_dir / src["file"]
    a_in, a_out, _ = source_span(item, src)
    expect = max(1, round((a_out - a_in) * OVERVIEW_RATE))
    out = []
    for k in range(_audio_streams(media, path)):
        peak = power = None
        pos = 0
        for x in _pcm(media, path, a_in, a_out - a_in, k, rate=OVERVIEW_RATE, cancelled=cancelled):
            if peak is None:
                peak = np.zeros((OVERVIEW_BUCKETS, x.shape[1]))
                power = np.zeros((OVERVIEW_BUCKETS, x.shape[1]))
            idx = np.minimum(np.arange(pos, pos + len(x)) * OVERVIEW_BUCKETS // expect, OVERVIEW_BUCKETS - 1)
            cuts = np.flatnonzero(np.r_[True, idx[1:] != idx[:-1]])
            at = idx[cuts]  # each bucket at most once per block
            peak[at] = np.maximum(peak[at], np.maximum.reduceat(np.abs(x), cuts))
            power[at] += np.add.reduceat(x.astype(np.float64) ** 2, cuts)
            pos += len(x)
        channels = []
        if peak is not None:
            bars = np.clip((20 * np.log10(peak + 1e-10) - OVERVIEW_FLOOR_DB) / -OVERVIEW_FLOOR_DB, 0, 1) * 100
            for ch in range(peak.shape[1]):
                top, energy = float(peak[:, ch].max()), float(power[:, ch].sum())
                channels.append({"peaks": [round(v) for v in bars[:, ch]],
                                 "peakDb": round(20 * math.log10(top), 1) if top > 0 else None,
                                 "rmsDb": round(10 * math.log10(energy / pos), 1) if energy > 0 else None})
        out.append({"stream": k, "channels": channels})
    return out
