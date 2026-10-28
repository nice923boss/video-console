"""Keyframes: values that change over a clip.

A clip's "keyframes" list holds {t, <field>: value, ...}; t is seconds into the clip. Between two keyframes
that set a field its value moves in a straight line, before the first and after the last it holds, and a
clip without keyframes for a field keeps its plain value. Grades blend number by number. The preview uses
the same rules (app/web/js/lib/keyframes.js).
"""
from __future__ import annotations

import math
from typing import Any


def points(item: dict, field: str) -> list[tuple[float, Any]]:
    """(t, value) of the keyframes that set field, in time order."""
    return [(float(k["t"]), k[field]) for k in item.get("keyframes") or [] if field in k]


def lerp(a: Any, b: Any, f: float) -> Any:
    if isinstance(a, dict):
        return {k: lerp(a[k], b[k], f) for k in a}
    return a + (b - a) * f


def value_at(item: dict, field: str, t: float, default: Any = None) -> Any:
    """The field's value t seconds into the clip."""
    pts = points(item, field)
    if not pts:
        return item.get(field, default)
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t < t1:
            return lerp(v0, v1, (t - t0) / (t1 - t0))
    return pts[-1][1]


def expr(item: dict, field: str, var: str, offset: float = 0.0, default: float = 0.0) -> str:
    """An ffmpeg expression for the field's value, where var - offset is the time into the clip."""
    pts = points(item, field)
    if not pts:
        return f"{float(item.get(field, default)):.4f}"
    x = f"({var}-{offset:.3f})" if offset else var
    out = f"{float(pts[-1][1]):.4f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        seg = f"{float(v0):.4f}+({float(v1) - float(v0):.4f})*({x}-{t0:.3f})/{t1 - t0:.3f}"
        out = f"if(lt({x},{t1:.3f}),{seg},{out})"
    return f"if(lt({x},{pts[0][0]:.3f}),{float(pts[0][1]):.4f},{out})"


def steps(item: dict, field: str, start: float, end: float, fps: int, max_steps: int = 30) -> list[tuple[float, float, Any]]:
    """(from, to, value) spans covering start..end (timeline seconds) with the field's value held in each.

    Export uses this for values a filter cannot animate (a grade baked into a LUT): every change between two
    keyframes is cut into at most max_steps even steps, at most one per frame.
    """
    pts = [(start + t, v) for t, v in points(item, field)]
    if not pts:
        return [(start, end, item.get(field))]
    spans: list[tuple[float, float, Any]] = []
    first = min(max(pts[0][0], start), end)
    if first > start:
        spans.append((start, first, pts[0][1]))
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        a, b = max(t0, start), min(t1, end)
        if b <= a:
            continue
        n = 1 if v0 == v1 else max(1, min(max_steps, math.ceil((b - a) * fps)))
        for i in range(n):
            s, e = a + (b - a) * i / n, a + (b - a) * (i + 1) / n
            mid = ((s + e) / 2 - t0) / (t1 - t0)
            spans.append((s, e, lerp(v0, v1, mid) if n > 1 else v0))
    last = max(min(pts[-1][0], end), start)
    if last < end:
        spans.append((last, end, pts[-1][1]))
    return spans


def source_keys(keys: list[dict], t_in: float, speed: float, length: float) -> list[dict]:
    """A take's volume keys (t in file seconds) as clip keyframes: t = (file seconds - t_in) / speed, cut to 0..length."""
    if not keys:
        return []
    item = {"keyframes": keys}
    out = {0.0: float(value_at(item, "volume", t_in, 1.0)),
           round(length, 3): float(value_at(item, "volume", t_in + length * speed, 1.0))}
    for k in keys:
        t = round((float(k["t"]) - t_in) / speed, 3)
        if 0 < t < round(length, 3):
            out[t] = float(k["volume"])
    return [{"t": t, "volume": round(v, 4)} for t, v in sorted(out.items())]
