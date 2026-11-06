"""Colour grading for adjustment layers, after Final Cut Pro's colour wheels.

A grade has four wheels (global, shadows, midtones, highlights), each a colour offset (r, g, b) plus
saturation and brightness, and on top of that temperature, tint, hue and a mix amount. The pixel model
here is the reference: the preview builds an SVG filter from the same steps (app/web/js/lib/grade.js)
and export bakes it into a .cube LUT for ffmpeg's lut3d. Values are sRGB 0..1, clamped after each step.

  1. temperature/tint gains, then a hue rotation (one 3x3 matrix)
  2. per channel: offset (global), lift (shadows), gain (highlights), gamma (midtones)
  3. global saturation around Rec.709 luma
  4. saturation by tonal range: s(L) blends the shadow, midtone and highlight saturation
  5. mix with the untouched picture

Before all of that sits an optional white balance block (grade["wb"], after the FCPX "White Balance" plugin):
the colour of a sampled patch that should be neutral grey is turned into channel gains that make it grey,
optionally with an exposure gain that brings the patch to a target level, then temperature, tint, hue and
saturation, then a tone curve (blacks, lift, gain, gamma, fill light, brightness, contrast), all weighted by
tonal range and a mix. The main grade then works on that balanced picture. Every field is a number, so
keyframes interpolate it like the rest of the grade.
"""
from __future__ import annotations

import math
from typing import Any, Callable

from .util import AppError

WHEELS = ("global", "shadows", "midtones", "highlights")
NEUTRAL_WHEEL = {"r": 0.0, "g": 0.0, "b": 0.0, "sat": 1.0, "bright": 0.0}
WHEEL_LIMITS = {"r": (-1.0, 1.0), "g": (-1.0, 1.0), "b": (-1.0, 1.0), "sat": (0.0, 2.0), "bright": (-1.0, 1.0)}
NEUTRAL_REST = {"temp": 5000.0, "tint": 0.0, "hue": 0.0, "mix": 1.0}
REST_LIMITS = {"temp": (2500.0, 10000.0), "tint": (-100.0, 100.0), "hue": (-180.0, 180.0), "mix": (0.0, 1.0)}
LUMA = (0.2126, 0.7152, 0.0722)
# White balance block. r/g/b: the sampled colour that should be neutral; x/y/size: where it was sampled
# (picture fractions, kept so the panel can show the spot again); amount: how much of the neutralising gain
# is applied; exposure/level: how much the patch is pulled to the target brightness `level`.
NEUTRAL_WB = {
    "r": 0.5, "g": 0.5, "b": 0.5, "x": 0.5, "y": 0.5, "size": 0.1, "amount": 1.0,
    "exposure": 0.0, "level": 0.5, "temp": 0.0, "tint": 0.0, "hue": 0.0, "sat": 0.0,
    "blacks": 0.0, "lift": 0.0, "gain": 0.0, "gamma": 0.0, "fill": 0.0, "bright": 0.0, "contrast": 0.0,
    "shadows": 1.0, "midtones": 1.0, "highlights": 1.0, "mix": 1.0,
}
WB_LIMITS = {
    "r": (0.01, 1.0), "g": (0.01, 1.0), "b": (0.01, 1.0), "x": (0.0, 1.0), "y": (0.0, 1.0), "size": (0.01, 1.0),
    "amount": (0.0, 1.0), "exposure": (0.0, 1.0), "level": (0.05, 0.95), "temp": (-100.0, 100.0),
    "tint": (-100.0, 100.0), "hue": (-180.0, 180.0), "sat": (-1.0, 1.0), "blacks": (-1.0, 1.0),
    "lift": (-1.0, 1.0), "gain": (-1.0, 1.0), "gamma": (-1.0, 1.0), "fill": (-1.0, 1.0), "bright": (-1.0, 1.0),
    "contrast": (-1.0, 1.0), "shadows": (0.0, 1.0), "midtones": (0.0, 1.0), "highlights": (0.0, 1.0),
    "mix": (0.0, 1.0),
}
# Fields that change pixels (x/y/size only record where the sample was taken).
WB_ACTIVE = tuple(k for k in NEUTRAL_WB if k not in ("x", "y", "size"))
GAIN_LIMITS = (0.25, 4.0)


def default_grade() -> dict:
    return {**{w: dict(NEUTRAL_WHEEL) for w in WHEELS}, **NEUTRAL_REST, "wb": dict(NEUTRAL_WB)}


def _num(v: Any, lo: float, hi: float, default: float) -> float:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise AppError("調色資料格式不正確")
    return round(max(lo, min(hi, float(v))), 4)


def clean_grade(v: Any) -> dict:
    """Validate a grade: missing fields take their neutral value and numbers are clamped to range."""
    if v is None:
        return default_grade()
    if not isinstance(v, dict) or set(v) - set(WHEELS) - set(NEUTRAL_REST) - {"wb"}:
        raise AppError("調色資料格式不正確")
    out: dict = {}
    for w in WHEELS:
        wheel = v.get(w) or {}
        if not isinstance(wheel, dict) or set(wheel) - set(NEUTRAL_WHEEL):
            raise AppError("調色資料格式不正確")
        out[w] = {k: _num(wheel.get(k), *WHEEL_LIMITS[k], NEUTRAL_WHEEL[k]) for k in NEUTRAL_WHEEL}
    for k, (lo, hi) in REST_LIMITS.items():
        out[k] = _num(v.get(k), lo, hi, NEUTRAL_REST[k])
    out["wb"] = clean_wb(v.get("wb"))
    return out


def clean_wb(v: Any) -> dict:
    """Validate the white balance block the same way: missing fields neutral, numbers clamped."""
    v = v or {}
    if not isinstance(v, dict) or set(v) - set(NEUTRAL_WB):
        raise AppError("白平衡資料格式不正確")
    return {k: _num(v.get(k), *WB_LIMITS[k], NEUTRAL_WB[k]) for k in NEUTRAL_WB}


def wb_neutral(wb: dict | None) -> bool:
    """True when the white balance block leaves every pixel as it is."""
    if not wb:
        return True
    full = {**NEUTRAL_WB, **wb}
    if full["mix"] <= 0:
        return True
    gains = wb_gains(full)
    rest = all(full[k] == NEUTRAL_WB[k] for k in WB_ACTIVE if k not in ("r", "g", "b", "amount", "exposure", "level"))
    return rest and all(abs(x - 1) < 1e-6 for x in gains)


def main_neutral(grade: dict) -> bool:
    """True when the colour wheels and the rest of the main grade change nothing (the wb block aside)."""
    if grade["mix"] <= 0:
        return True
    base = default_grade()
    return all(grade.get(k) == base[k] for k in base if k != "wb")


def is_neutral(grade: dict) -> bool:
    return main_neutral(grade) and wb_neutral(grade.get("wb"))


def _clamp(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def color_matrix(grade: dict) -> list[list[float]]:
    """Temperature and tint as channel gains, followed by the W3C hueRotate matrix."""
    w = math.log2(grade["temp"] / 5000.0)
    k = grade["tint"] / 100.0
    gains = (1 + 0.3 * w, 1 - 0.3 * k, 1 - 0.3 * w)
    a = math.radians(grade["hue"])
    c, s = math.cos(a), math.sin(a)
    hue = (
        (0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928),
        (0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.140, 0.072 - c * 0.072 - s * 0.283),
        (0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072),
    )
    return [[hue[i][j] * gains[j] for j in range(3)] for i in range(3)]


def channel_curves(grade: dict) -> list[Callable[[float], float]]:
    """One tone curve per channel: offset, lift, gain, then a midtone gamma."""
    sh, mid, hi, gl = grade["shadows"], grade["midtones"], grade["highlights"], grade["global"]
    curves = []
    for ch in "rgb":
        lift = 0.25 * (sh[ch] + sh["bright"])
        gain = 1 + 0.5 * (hi[ch] + hi["bright"])
        power = 2 ** -(mid[ch] + mid["bright"])
        off = 0.3 * (gl[ch] + gl["bright"])

        def curve(x: float, lift: float = lift, gain: float = gain, power: float = power, off: float = off) -> float:
            return _clamp(_clamp(lift + _clamp(x + off) * (gain - lift)) ** power)

        curves.append(curve)
    return curves


def range_saturation(grade: dict, luma: float) -> float:
    """Saturation for a pixel of this brightness, blended from the shadow, midtone and highlight wheels."""
    return ((1 - luma) ** 2 * grade["shadows"]["sat"] + 2 * luma * (1 - luma) * grade["midtones"]["sat"]
            + luma ** 2 * grade["highlights"]["sat"])


def _luma(c) -> float:
    return LUMA[0] * c[0] + LUMA[1] * c[1] + LUMA[2] * c[2]


def wb_gains(wb: dict) -> tuple[float, float, float]:
    """Channel gains that turn the sampled colour grey (amount 1) and, with exposure, bring it to `level`."""
    s = (wb["r"], wb["g"], wb["b"])
    ls = _luma(s)
    expo = 1 + wb["exposure"] * (wb["level"] / max(ls, 0.01) - 1)
    lo, hi = GAIN_LIMITS
    r, g, b = (max(lo, min(hi, (1 + wb["amount"] * (ls / max(c, 0.01) - 1)) * expo)) for c in s)
    return r, g, b


def wb_matrix(wb: dict) -> list[list[float]]:
    """Neutralising gains, temperature/tint gains, hue rotation and saturation as one 3x3 matrix."""
    n = wb_gains(wb)
    w, k = wb["temp"] / 100.0, wb["tint"] / 100.0
    gains = (n[0] * (1 + 0.3 * w), n[1] * (1 - 0.3 * k), n[2] * (1 - 0.3 * w))
    hue = color_matrix({"temp": 5000.0, "tint": 0.0, "hue": wb["hue"]})
    s = 1 + wb["sat"]
    sat = [[(s if i == j else 0.0) + (1 - s) * LUMA[j] for j in range(3)] for i in range(3)]
    sh = [[sum(sat[i][t] * hue[t][j] for t in range(3)) for j in range(3)] for i in range(3)]
    return [[sh[i][j] * gains[j] for j in range(3)] for i in range(3)]


def wb_curve(wb: dict) -> Callable[[float], float]:
    """The white balance tone curve, the same for all three channels."""
    blk, lift, gain, power = wb["blacks"] * 0.1, wb["lift"] * 0.25, 1 + 0.5 * wb["gain"], 2 ** -wb["gamma"]
    fill, bright, contrast = 1.2 * wb["fill"], 0.25 * wb["bright"], 2 ** wb["contrast"]

    def curve(x: float) -> float:
        x = _clamp((x - blk) / (1 - blk))
        x = _clamp(x + lift * (1 - x))
        x = _clamp(x * gain)
        x = _clamp(x ** power)
        x = _clamp(x + fill * x * (1 - x) ** 2)
        x = _clamp(x + bright)
        return _clamp(0.5 + (x - 0.5) * contrast)

    return curve


def wb_weight(wb: dict, luma: float) -> float:
    """How much of the white balance a pixel of this brightness takes, mix included."""
    return wb["mix"] * ((1 - luma) ** 2 * wb["shadows"] + 2 * luma * (1 - luma) * wb["midtones"]
                        + luma ** 2 * wb["highlights"])


def wb_ranged(wb: dict) -> bool:
    return any(wb[k] != 1 for k in ("shadows", "midtones", "highlights"))


def wb_apply(wb: dict | None) -> Callable[[float, float, float], tuple[float, float, float]] | None:
    """A function mapping one sRGB pixel through the white balance block, or None when it changes nothing."""
    if wb_neutral(wb):
        return None
    wb = {**NEUTRAL_WB, **(wb or {})}
    m = wb_matrix(wb)
    curve = wb_curve(wb)
    ranged = wb_ranged(wb)
    mix = wb["mix"]

    def apply(r: float, g: float, b: float) -> tuple[float, float, float]:
        t = [curve(_clamp(m[i][0] * r + m[i][1] * g + m[i][2] * b)) for i in range(3)]
        k = wb_weight(wb, _luma((r, g, b))) if ranged else mix
        return (r + k * (t[0] - r), g + k * (t[1] - g), b + k * (t[2] - b))

    return apply


def grader(grade: dict) -> Callable[[float, float, float], tuple[float, float, float]]:
    """A function mapping one sRGB pixel through the grade (the white balance block first)."""
    balance = wb_apply(grade.get("wb"))
    main = _main_grader(grade)
    if balance is None:
        return main
    return lambda r, g, b: main(*balance(r, g, b))


def _main_grader(grade: dict) -> Callable[[float, float, float], tuple[float, float, float]]:
    m = color_matrix(grade)
    curves = channel_curves(grade)
    gsat = grade["global"]["sat"]
    mix = grade["mix"]
    ranged = any(grade[w]["sat"] != 1 for w in ("shadows", "midtones", "highlights"))

    def apply(r: float, g: float, b: float) -> tuple[float, float, float]:
        c = [_clamp(m[i][0] * r + m[i][1] * g + m[i][2] * b) for i in range(3)]
        c = [curves[i](c[i]) for i in range(3)]
        lum = LUMA[0] * c[0] + LUMA[1] * c[1] + LUMA[2] * c[2]
        c = [_clamp(lum + gsat * (x - lum)) for x in c]
        if ranged:
            lum = LUMA[0] * c[0] + LUMA[1] * c[1] + LUMA[2] * c[2]
            k = range_saturation(grade, lum) - 1
            c = [_clamp(x + k * (x - lum)) for x in c]
        return (mix * c[0] + (1 - mix) * r, mix * c[1] + (1 - mix) * g, mix * c[2] + (1 - mix) * b)

    return apply


def build_cube(grade: dict, size: int = 33) -> str:
    """The grade as an Adobe .cube 3D LUT (red changes fastest)."""
    apply = grader(grade)
    step = 1 / (size - 1)
    lines = ['TITLE "Video Console grade"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                r, g, b = apply(ri * step, gi * step, bi * step)
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    return "\n".join(lines) + "\n"
