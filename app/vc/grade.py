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


def default_grade() -> dict:
    return {**{w: dict(NEUTRAL_WHEEL) for w in WHEELS}, **NEUTRAL_REST}


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
    if not isinstance(v, dict) or set(v) - set(WHEELS) - set(NEUTRAL_REST):
        raise AppError("調色資料格式不正確")
    out: dict = {}
    for w in WHEELS:
        wheel = v.get(w) or {}
        if not isinstance(wheel, dict) or set(wheel) - set(NEUTRAL_WHEEL):
            raise AppError("調色資料格式不正確")
        out[w] = {k: _num(wheel.get(k), *WHEEL_LIMITS[k], NEUTRAL_WHEEL[k]) for k in NEUTRAL_WHEEL}
    for k, (lo, hi) in REST_LIMITS.items():
        out[k] = _num(v.get(k), lo, hi, NEUTRAL_REST[k])
    return out


def is_neutral(grade: dict) -> bool:
    return grade["mix"] <= 0 or grade == default_grade()


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


def grader(grade: dict) -> Callable[[float, float, float], tuple[float, float, float]]:
    """A function mapping one sRGB pixel through the grade."""
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
