"""Clip compressor after the Logic Compressor in Final Cut Pro. Mirror of web/js/lib/compressor.js: both compute
the gain once per BLOCK samples by the same formulas, so the preview (compressor-worklet.js) and the export agree.

Signal flow per clip:
  in gain -> [detector: peak/RMS, stereo link] -> gain computer (threshold, ratio, knee)
  -> attack/release smoothing (dB domain) -> lookahead delay -> make up (+auto gain)
  -> circuit colour -> distortion -> dry/wet mix -> limiter -> out gain

The export plays a clip with the compressor on from a render of it (media/dyn/<id>-<hash>.wav): the clip's span
of its file (or of its 降噪 / 去回音 stems, remixed), at its speed, through Compressor, as 32-bit float. The hash
covers the file, the span, the speed, the remix and every setting, so an unchanged clip is not rendered again.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .media import NO_WINDOW, Media
from .sequence import audio_media
from .util import AppError
from .voiceclean import _num, mix_lines, source_span, stems_for

RATE = 48000
BLOCK = 32  # control rate: one gain value per 32 samples (0.67 ms at 48 kHz)
LOOKAHEAD = 240  # 5 ms, a multiple of BLOCK
KNEE_MAX_DB = 20.0  # Knee 1.0 = a 20 dB wide soft knee (design choice; Apple does not publish it)
RMS_MS = 10.0
LIMIT_RELEASE_MS = 50.0
AUTO_AVG_MS = 1000.0  # how long the auto release remembers sustained reduction
FLOOR = 1e-9
DSP_VERSION = 1  # part of every rendered file's name: a change to the formulas must bump it
DYN_DIR = "media/dyn"
CHUNK = RATE * 4  # samples read from ffmpeg per step of a render
AFORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

# Internal behaviour per circuit. Index = the value FCPX stores (0 Platinum ... 6 Vintage Opto).
CIRCUITS = (
    {"key": "platinum", "feedback": False, "rms": None, "knee_min": 0.0, "color": 0.0, "kind": "odd", "auto_k": 0.0},
    {"key": "studio_vca", "feedback": False, "rms": True, "knee_min": 0.0, "color": 0.08, "kind": "even", "auto_k": 0.0},
    {"key": "studio_fet", "feedback": True, "rms": False, "knee_min": 0.0, "color": 0.15, "kind": "odd", "auto_k": 0.0},
    {"key": "classic_vca", "feedback": False, "rms": True, "knee_min": 0.0, "color": 0.05, "kind": "even", "auto_k": 2.0},
    {"key": "vintage_vca", "feedback": False, "rms": True, "knee_min": 0.4, "color": 0.12, "kind": "even", "auto_k": 1.0},
    {"key": "vintage_fet", "feedback": True, "rms": False, "knee_min": 0.0, "color": 0.25, "kind": "odd", "auto_k": 0.0},
    {"key": "vintage_opto", "feedback": True, "rms": True, "knee_min": 0.6, "color": 0.1, "kind": "even", "auto_k": 6.0},
)

DEFAULT_COMP: dict[str, Any] = {
    "on": True, "circuit": 0, "threshold": -20.0, "ratio": 2.0, "knee": 0.5, "attack": 15.0, "release": 100.0,
    "autoRelease": False, "makeup": 0.0, "autoGain": 0, "rms": False, "link": "max", "lookahead": False,
    "limiter": False, "limiterThreshold": 0.0, "distortion": "off", "mix": 1.0, "inGain": 0.0, "outGain": 0.0,
}
RANGES = {
    "threshold": (-60.0, 0.0), "ratio": (1.0, 30.0), "knee": (0.0, 1.0), "attack": (0.0, 200.0),
    "release": (5.0, 5000.0), "makeup": (-30.0, 30.0), "limiterThreshold": (-20.0, 0.0), "mix": (0.0, 1.0),
    "inGain": (-30.0, 30.0), "outGain": (-30.0, 30.0),
}
DISTORTIONS = ("off", "soft", "hard", "clip")
_BOOLS = ("on", "autoRelease", "rms", "lookahead", "limiter")


def clean_comp(v: Any) -> dict | None:
    """The `comp` field of an audio clip or a shot as an edit sends it: missing fields take their default,
    numbers are clamped to their range and an unknown circuit, auto gain, link or distortion falls back to the
    default, as normalize does; None drops it."""
    if v is None:
        return None
    message = "壓縮器設定格式不正確"
    if not isinstance(v, dict) or set(v) - set(DEFAULT_COMP):
        raise AppError(message)
    out: dict = {}
    for k, d in DEFAULT_COMP.items():
        x = v.get(k)
        if k in _BOOLS:
            if x is not None and not isinstance(x, bool):
                raise AppError(message)
            out[k] = d if x is None else x
        elif k in RANGES:
            out[k] = _num(x, *RANGES[k], d, message)
        elif k in ("circuit", "autoGain"):
            n = _num(x, -1e9, 1e9, d, message)
            out[k] = int(n) if n == int(n) and int(n) in range(len(CIRCUITS) if k == "circuit" else 3) else d
        else:
            if x is not None and not isinstance(x, str):
                raise AppError(message)
            out[k] = x if x in (("max", "sum") if k == "link" else DISTORTIONS) else d
    return out


def normalize(raw: dict | None) -> dict:
    """A clip's `comp` field, clamped and filled in; never trusts what the file holds."""
    p = {**DEFAULT_COMP, **(raw or {})}
    for k, (lo, hi) in RANGES.items():
        p[k] = min(hi, max(lo, float(p[k])))
    p["circuit"] = int(p["circuit"]) if int(p["circuit"]) in range(len(CIRCUITS)) else 0
    p["autoGain"] = int(p["autoGain"]) if int(p["autoGain"]) in (0, 1, 2) else 0
    p["link"] = "sum" if p["link"] == "sum" else "max"
    p["distortion"] = p["distortion"] if p["distortion"] in DISTORTIONS else "off"
    return p


def comp_on(item: dict) -> dict | None:
    """The clip's compressor settings when it is switched on, else None."""
    comp = item.get("comp")
    return comp if isinstance(comp, dict) and comp.get("on") else None


def db(x: float) -> float:
    return 20.0 * math.log10(max(x, FLOOR))


def knee_db(p: dict) -> float:
    return KNEE_MAX_DB * max(p["knee"], CIRCUITS[p["circuit"]]["knee_min"])


def gain_computer(x: float, t: float, r: float, w: float) -> float:
    """Static curve: output level (dB) for input level x (dB); soft knee of width w (Giannoulis et al. 2012)."""
    over = x - t
    if 2 * over < -w:
        return x
    if w > 0 and 2 * abs(over) <= w:
        return x + (1 / r - 1) * (over + w / 2) ** 2 / (2 * w)
    return t + over / r


def auto_makeup(p: dict) -> float:
    """Auto Gain 0 dB: a 0 dBFS input leaves at 0 dBFS; -12 dB: a -12 dBFS input leaves at -12 dBFS (approximation)."""
    if not p["autoGain"]:
        return 0.0
    ref = 0.0 if p["autoGain"] == 1 else -12.0
    return ref - gain_computer(ref, p["threshold"], p["ratio"], knee_db(p))


def coef(ms: float) -> float:
    """One-pole coefficient per BLOCK for a time constant in ms."""
    return math.exp(-BLOCK / (max(ms, 0.01) * 1e-3 * RATE))


def _np() -> Any:
    import numpy  # only the compressor render needs it

    return numpy


def saturate(v: Any, kind: str, amt: float) -> Any:
    """Circuit colour; unity gain for small signals. Works on numpy arrays."""
    if amt <= 0:
        return v
    np = _np()
    if kind == "odd":
        return (1 - amt) * v + amt * np.tanh(v)
    b = 0.25
    return (1 - amt) * v + amt * (np.tanh(v + b) - math.tanh(b)) / (1 - math.tanh(b) ** 2)


def distort(v: Any, mode: str) -> Any:
    np = _np()
    if mode == "soft":
        return np.tanh(v)
    if mode == "hard":
        c = np.clip(v, -1.0, 1.0)
        return 1.5 * c - 0.5 * c ** 3
    if mode == "clip":
        return np.clip(v, -1.0, 1.0)
    return v


def _ramp(prev: Any, cur: Any) -> Any:
    """Per-sample values going linearly from each block's previous value to its own, flattened."""
    np = _np()
    k = np.arange(1, BLOCK + 1) / BLOCK
    return (prev[:, None] + (cur - prev)[:, None] * k[None, :]).reshape(-1)


class Compressor:
    """One clip's compressor over its sound fed in pieces of any length (float64 arrays (2, n) at RATE); what
    comes out is in step with what went in (the lookahead's delay is dropped, as an offline render can look ahead
    for free) and as long, once flush() has given the tail. trace=True keeps the reduction per block (meters, tests)."""

    def __init__(self, raw: dict | None, trace: bool = False) -> None:
        np = _np()
        p = self.p = normalize(raw)
        c = self.c = CIRCUITS[p["circuit"]]
        self.use_rms = p["rms"] if c["rms"] is None else c["rms"]
        self.w = knee_db(p)
        self.a_att, self.a_rms, self.a_avg = coef(p["attack"]), coef(RMS_MS), coef(AUTO_AVG_MS)
        self.auto_k = c["auto_k"] if c["auto_k"] else (1.0 if p["autoRelease"] else 0.0)
        self.in_g, self.out_g = 10 ** (p["inGain"] / 20), 10 ** (p["outGain"] / 20)
        self.makeup = 10 ** ((p["makeup"] + auto_makeup(p)) / 20)
        self.thr = 10 ** (p["limiterThreshold"] / 20)
        self.la = LOOKAHEAD if p["lookahead"] else 0
        self.hist = np.zeros((2, self.la))  # the last la input samples: the delay line
        self.rest = np.zeros((2, 0))  # input short of a whole block
        self.gs = self.msq = self.avg = self.prev_db = 0.0
        self.lim = 1.0
        self.fed = self.made = self.sent = 0  # samples fed, worked out (delayed) and given back
        self.trace: list[float] | None = [] if trace else None

    def _track(self, peak: Any, ms: Any) -> list[float]:
        """Gain reduction (dB, <= 0) per block from each block's linked detector values."""
        p, c = self.p, self.c
        t, r, w = p["threshold"], p["ratio"], self.w
        a_att, a_rms, a_avg, auto_k = self.a_att, self.a_rms, self.a_avg, self.auto_k
        a_fixed = coef(p["release"]) if not auto_k else None
        gs, msq, avg = self.gs, self.msq, self.avg
        out: list[float] = []
        for pk, m in zip(peak.tolist(), ms.tolist()):
            msq = a_rms * msq + (1 - a_rms) * m
            level = db(math.sqrt(msq)) if self.use_rms else db(pk)
            if c["feedback"]:
                level += gs  # feedback circuits hear their own output: input level plus the reduction so far
            gc = gain_computer(level, t, r, w) - level
            avg = a_avg * avg + (1 - a_avg) * gs
            a_rel = a_fixed if a_fixed is not None else coef(p["release"] * (1 + auto_k * min(1.0, -avg / 12.0)))
            a = a_att if gc < gs else a_rel
            gs = a * gs + (1 - a) * gc
            out.append(gs)
        self.gs, self.msq, self.avg = gs, msq, avg
        return out

    def _limit(self, wet: Any) -> Any:
        np = _np()
        peaks = np.abs(wet).reshape(2, -1, BLOCK).max(axis=(0, 2))
        a_rel = coef(LIMIT_RELEASE_MS)
        g, gains = self.lim, []
        for pk in peaks.tolist():
            need = min(1.0, self.thr / max(pk, FLOOR))
            g = need if need < g else a_rel * g + (1 - a_rel) * min(1.0, need)
            gains.append(g)
        cur = np.array(gains)
        prev = np.concatenate([[self.lim], cur[:-1]])
        self.lim = g
        return np.clip(wet * _ramp(prev, cur)[None, :], -self.thr, self.thr)

    def _blocks(self, xs: Any) -> Any:
        """Whole blocks of input in, as many samples out, `la` late."""
        np = _np()
        p = self.p
        m = xs.shape[1]
        if not m:
            return np.zeros((2, 0))
        xin = xs * self.in_g
        blocks = xin.reshape(2, -1, BLOCK)
        if p["link"] == "sum":
            mono = blocks.mean(axis=0)
            peak, ms = np.abs(mono).max(axis=1), (mono ** 2).mean(axis=1)
        else:
            peak, ms = np.abs(blocks).max(axis=2).max(axis=0), (blocks ** 2).mean(axis=2).max(axis=0)
        gr = self._track(peak, ms)
        if self.trace is not None:
            self.trace.extend(gr)
        cur = np.array(gr)
        prev = np.concatenate([[self.prev_db], cur[:-1]])
        self.prev_db = gr[-1]
        g_db = _ramp(prev, cur)
        if self.la:  # the audio is late by la; the gain is not, so it starts falling before the transient
            line = np.concatenate([self.hist, xs], axis=1)
            dx, self.hist = line[:, :m], line[:, m:]
        else:
            dx = xs
        wet = dx * self.in_g * (10 ** (g_db / 20))[None, :] * self.makeup
        wet = distort(saturate(wet, self.c["kind"], self.c["color"]), p["distortion"])
        out = dx * (1 - p["mix"]) + wet * p["mix"]
        if p["limiter"]:
            out = self._limit(out)
        return out * self.out_g

    def _give(self, out: Any) -> Any:
        """Drops the lookahead's first la samples and anything past the input's length."""
        lo = max(0, self.la - self.made)
        self.made += out.shape[1]
        out = out[:, lo:]
        out = out[:, :max(0, self.fed - self.sent)]
        self.sent += out.shape[1]
        return out

    def feed(self, x: Any) -> Any:
        np = _np()
        self.fed += x.shape[1]
        buf = np.concatenate([self.rest, x], axis=1)
        m = buf.shape[1] // BLOCK * BLOCK
        self.rest = buf[:, m:]
        return self._give(self._blocks(buf[:, :m]))

    def flush(self) -> Any:
        """The rest of the output, made by feeding silence after the input."""
        np = _np()
        need = self.fed + self.la - self.made  # samples still to work out, the input left over among them
        pad = max(0, need - self.rest.shape[1])
        buf = np.concatenate([self.rest, np.zeros((2, pad))], axis=1)
        buf = np.pad(buf, ((0, 0), (0, (-buf.shape[1]) % BLOCK)))
        self.rest = np.zeros((2, 0))
        return self._give(self._blocks(buf))


def process(x: Any, raw: dict | None) -> tuple[Any, list[float]]:
    """x: float64 array (2, n) at RATE. Returns the processed audio (2, n) and the reduction per block."""
    np = _np()
    comp = Compressor(raw, trace=True)
    out = np.concatenate([comp.feed(x), comp.flush()], axis=1)
    return out, comp.trace or []


# ------------------------------------------------------------ the render the export plays

def dyn_job(project: dict, project_dir: Path, item: dict, speed: float, tempo: str) -> dict | None:
    """What the render of an audio clip (or a shot's own sound, voiceclean.shot_sound) with the compressor on
    reads and where it goes: {"file", "args", "graph", "comp"}, or None when it is off or the file is missing.
    tempo: export.py's atempo filters for the speed (with their trailing comma), so the render plays the clip
    at its speed before the compressor, as the preview does."""
    comp = comp_on(item)
    src = audio_media(project, item)
    if not comp or not src or not (project_dir / src["file"]).is_file():
        return None
    a_in, a_out, _ = source_span(item, src)
    if a_out - a_in <= 0:
        return None
    dur = a_out - a_in
    stems = stems_for(project, project_dir, item)
    if stems:  # the 降噪 / 去回音 / one channel remix of its stems, as _plan_audio plays them
        file = stems["file"]
        args = ["-i", file]
        graph = [*mix_lines(item, "0:a", "m"),
                 f"[m]{AFORMAT},atrim=start={a_in - stems['from']:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
                 f"{tempo}anull[o]"]
    else:
        file = src["file"]
        args = ["-ss", f"{a_in:.3f}", "-t", f"{dur:.3f}", "-i", file]
        graph = [f"[0:a]{AFORMAT},{tempo}anull[o]"]
    st = (project_dir / file).stat()
    p = normalize(comp)
    raw = json.dumps([DSP_VERSION, file, st.st_size, st.st_mtime_ns, args, graph, round(speed, 4), p], sort_keys=True)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return {"file": f"{DYN_DIR}/{item['id']}-{digest}.wav", "args": args, "graph": graph, "comp": p}


def _wav_header(frames: int) -> bytes:
    """A 32-bit float stereo WAV header at RATE for `frames` frames."""
    data = frames * 8
    fmt = ((3).to_bytes(2, "little") + (2).to_bytes(2, "little") + RATE.to_bytes(4, "little")
           + (RATE * 8).to_bytes(4, "little") + (8).to_bytes(2, "little") + (32).to_bytes(2, "little"))
    return (b"RIFF" + (36 + data).to_bytes(4, "little") + b"WAVE" + b"fmt " + len(fmt).to_bytes(4, "little") + fmt
            + b"data" + data.to_bytes(4, "little"))


def ensure_dyn(media: Media, project_dir: Path, job: dict, cancelled: Callable[[], bool] | None = None) -> Path:
    """Renders dyn_job's file unless it is there already; older renders of the same clip are removed."""
    out = project_dir / job["file"]
    if out.is_file():
        return out
    np = _np()
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(f".{out.stem}.{threading.get_ident()}.part")
    cmd = [media.require(), "-hide_banner", "-nostdin", "-loglevel", "error", *job["args"],
           "-filter_complex", ";".join(job["graph"]), "-map", "[o]", "-ac", "2", "-ar", str(RATE),
           "-f", "f64le", "pipe:1"]
    proc = subprocess.Popen(cmd, cwd=project_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    assert proc.stdout is not None and proc.stderr is not None
    err: list[bytes] = []
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    reader.start()
    try:
        comp = Compressor(job["comp"])
        frames = 0
        with open(part, "wb") as f:
            f.write(_wav_header(0))

            def put(y: Any) -> None:
                nonlocal frames
                f.write(np.ascontiguousarray(y.T, dtype="<f4").tobytes())
                frames += y.shape[1]

            while True:
                if cancelled and cancelled():
                    raise AppError("已取消", 499)
                raw = proc.stdout.read(CHUNK * 16)
                if not raw:
                    break
                x = np.frombuffer(raw[:len(raw) // 16 * 16], "<f8").reshape(-1, 2).T
                put(comp.feed(x))
            put(comp.flush())
            proc.wait()
            reader.join(timeout=2)
            if proc.returncode != 0:
                raise media._error(b"".join(err).decode("utf-8", "replace"))
            f.seek(0)
            f.write(_wav_header(frames))
        os.replace(part, out)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        part.unlink(missing_ok=True)
    prefix = out.name.rsplit("-", 1)[0]
    for old in out.parent.glob(f"{prefix}-*.wav"):
        if old.name != out.name:
            try:
                old.unlink()
            except OSError:  # still open in another export; the next render tries again
                pass
    return out
