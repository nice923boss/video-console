"""The 壓低爆音 button on the server, mirror of web/js/lib/declip.js: finds where a clip's sound passes full scale
and the volume dips that bring each part back under. Times are seconds into the clip."""
from __future__ import annotations

import math
from typing import Any, Callable

from .jscompat import jsround, positive, r3, value_at
from .opfields import MAX_KEYFRAMES

TARGET = 0.97  # loudest peak a dip leaves, a little under full scale
RAMP = 0.1  # seconds a dip takes to go down and to come back
PAD = 0.01  # at least this much quiet on each side of a loud bucket


def _loud_buckets(wave: dict, t_in: float, length: float, speed: float,
                  gain_at: Callable[[float], float]) -> list[dict]:
    """[{s, e, gain}]: one per wave bucket over full scale, padded by a bucket, sampled at each bucket's middle."""
    peaks = wave["peaks"]
    n = len(peaks)
    dur = float(wave["duration"])
    level = float(wave["level"])
    step = dur / n / speed
    pad = max(step, PAD)
    first = max(0, math.floor((t_in / dur) * n))
    last = min(n, math.ceil(((t_in + length * speed) / dur) * n))
    out = []
    for i in range(first, last):
        mid = (((i + 0.5) / n) * dur - t_in) / speed
        amp = ((peaks[i] or 0) / 100) * level * gain_at(min(length, max(0, mid)))
        if amp > 1:
            out.append({"s": max(0, mid - step / 2 - pad), "e": min(length, mid + step / 2 + pad),
                        "gain": TARGET / amp})
    return out


def _merge(spans: list[dict], gap: float) -> list[dict]:
    """Spans closer than gap become one, at the lower gain."""
    out: list[dict] = []
    for x in spans:
        prev = out[-1] if out else None
        if prev and x["s"] - prev["e"] < gap:
            out[-1] = {"s": prev["s"], "e": max(prev["e"], x["e"]), "gain": min(prev["gain"], x["gain"])}
        else:
            out.append(x)
    return out


def _dip_keys(spans: list[dict], length: float) -> list[dict]:
    """Volume multiplier keys: down over RAMP before each span, flat across it, back up over RAMP after."""
    keys: list[dict] = []
    for sp in spans:
        s, e = sp["s"], sp["e"]
        g = math.floor(sp["gain"] * 10000) / 10000
        keys += [*([{"t": r3(s - RAMP), "volume": 1}] if s - RAMP > 0 else []),
                 {"t": r3(s), "volume": g}, {"t": r3(e), "volume": g},
                 *([{"t": r3(e + RAMP), "volume": 1}] if e + RAMP < length else [])]
    return keys


def declip_keys(wave: Any, t_in: float, length: float, speed: float, gain_at: Callable[[float], float],
                room: int = MAX_KEYFRAMES) -> dict | None:
    """declip: {spans, keys}, the loud parts and the multiplier keys (at most room) that bring them under full
    scale. The clip plays file seconds t_in + t * speed at gain_at(t). None when the wave has no level (made
    before media.py measured it)."""
    if (not isinstance(wave, dict) or not isinstance(wave.get("peaks"), list) or not wave["peaks"]
            or not positive(wave.get("level")) or not positive(wave.get("duration"))):
        return None
    gap = 2 * RAMP
    spans = _merge(_loud_buckets(wave, t_in, length, speed or 1, gain_at), gap)
    while len(spans) * 4 > room and len(spans) > 1:
        gap *= 2
        spans = _merge(spans, gap)
    return {"spans": spans, "keys": _dip_keys(spans, length)}


def with_dips(item: dict, dips: list[dict], length: float) -> list[dict]:
    """withDips: an audio clip's volume keyframes with the dips multiplied in, a key wherever either had one."""
    times = [k["t"] for k in item.get("keyframes") or [] if "volume" in k] + [k["t"] for k in dips]
    dip = {"keyframes": dips}
    return [{"t": t, "volume": min(4, jsround(float(value_at(item, "volume", t, 1))
                                              * float(value_at(dip, "volume", t, 1)), 4))}
            for t in sorted(dict.fromkeys(t for t in times if 0 <= t <= length))]


def play_gain(item: dict, t: float, length: float) -> float:
    """playGain in components/tl-wave.js: the volume (keyframes included) and fades at t seconds in."""
    fi = min(float(item.get("fadeIn") or 0), length / 2)
    fo = min(float(item.get("fadeOut") or 0), length / 2)
    gain = float(value_at(item, "volume", t, 1))
    if fi > 0 and t < fi:
        gain *= t / fi
    if fo > 0 and t > length - fo:
        gain *= (length - t) / fo
    return gain
