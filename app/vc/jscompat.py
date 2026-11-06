"""Small browser helpers that the server twins of edit actions (twins.py) share, written to give the same
numbers as their JS originals: Math.round rounds halves up, Number(undefined) is NaN, valueAt skips null."""
from __future__ import annotations

import math
import re
from typing import Any

_EXT = re.compile(r"\.[a-z0-9]{2,5}\Z", re.IGNORECASE | re.ASCII)


def jsround(v: float, digits: int) -> float:
    """Math.round(v * 10**digits) / 10**digits: halves go up, where Python's round goes to even."""
    f = 10 ** digits
    return math.floor(float(v) * f + 0.5) / f


def r3(v: float) -> float:
    """round(v, 3) of lib/format.js."""
    return jsround(v, 3)


def clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def js_number(item: dict, key: str) -> float:
    """Number(item[key]): a missing key is NaN, null is 0."""
    if key not in item:
        return math.nan
    try:
        return float(item[key] or 0)
    except (TypeError, ValueError):
        return math.nan


def positive(v: Any) -> bool:
    """Number(v) > 0."""
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def value_at(item: dict, field: str, t: float, fallback: float) -> Any:
    """valueAt in lib/keyframes.js for a number field: the keyframes in their stored order, else the field,
    and fallback when that is unset or null."""
    pts = [(float(k["t"]), k[field]) for k in item.get("keyframes") or [] if field in k]
    if not pts:
        v = item.get(field)
        return fallback if v is None else v
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t < t1:
            return v0 + (v1 - v0) * ((t - t0) / (t1 - t0))
    return pts[-1][1]


def stem(name: str | None) -> str:
    """A file name without its extension, at most 120 characters (stem in lib/media-actions.js)."""
    return _EXT.sub("", name or "", count=1)[:120]
