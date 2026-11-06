"""Compositing, transform, crop and distort of a shot's picture (ED-30), after the FCPX inspector.

Positions are fractions of the output frame (x of its width, y of its height) with y up, as FCPX shows them;
the panel shows them in frame pixels. The picture, already fitted to the frame and masked, is trimmed (the cut
edges turn black), its four corners moved (distort), then scaled around its anchor, rotated counterclockwise
and moved. The frame's corners end up on a quad, and one homography from the frame to that quad is the whole
geometry: export.py hands it to ffmpeg's perspective filter, the preview to CSS matrix3d
(web/js/lib/transform.js matches this file). Opacity fades the picture to the black under the main track.
"""
from __future__ import annotations

import math
from typing import Any

from .util import AppError

NEUTRAL = {"opacity": 1.0, "x": 0.0, "y": 0.0, "rotation": 0.0, "scale": 1.0, "scaleX": 1.0, "scaleY": 1.0,
           "anchorX": 0.0, "anchorY": 0.0}
LIMITS = {"opacity": (0.0, 1.0), "x": (-4.0, 4.0), "y": (-4.0, 4.0), "rotation": (-3600.0, 3600.0),
          "scale": (0.01, 10.0), "scaleX": (0.01, 10.0), "scaleY": (0.01, 10.0), "anchorX": (-4.0, 4.0),
          "anchorY": (-4.0, 4.0)}
SIDES = ("l", "r", "t", "b")
CROP_MAX = 0.98  # left + right (and top + bottom) leave at least 2% of the picture
CORNERS = ("tl", "tr", "br", "bl")
DISTORT_MAX = 4.0
PAD = 4  # black border the perspective filter samples past the picture's edges


def _num(v: Any, lo: float, hi: float, default: float) -> float:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise AppError("變換資料格式不正確")
    return round(max(lo, min(hi, float(v))), 5)


def _fields(v: Any, keys: tuple[str, ...]) -> dict:
    v = v or {}
    if not isinstance(v, dict) or set(v) - set(keys):
        raise AppError("變換資料格式不正確")
    return v


def clean_transform(v: Any) -> dict | None:
    """Validate a shot transform: missing fields neutral, numbers clamped; None when it leaves the picture as is."""
    if v is None:
        return None
    v = _fields(v, (*NEUTRAL, "crop", "distort"))
    out: dict = {k: _num(v.get(k), lo, hi, NEUTRAL[k]) for k, (lo, hi) in LIMITS.items()}
    c = _fields(v.get("crop"), SIDES)
    left, top = _num(c.get("l"), 0, CROP_MAX, 0.0), _num(c.get("t"), 0, CROP_MAX, 0.0)
    out["crop"] = {"l": left, "r": _num(c.get("r"), 0, CROP_MAX - left, 0.0),
                   "t": top, "b": _num(c.get("b"), 0, CROP_MAX - top, 0.0)}
    d = _fields(v.get("distort"), CORNERS)
    out["distort"] = {}
    for k in CORNERS:
        p = d.get(k) or [0, 0]
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise AppError("變形資料格式不正確")
        out["distort"][k] = [_num(p[0], -DISTORT_MAX, DISTORT_MAX, 0.0), _num(p[1], -DISTORT_MAX, DISTORT_MAX, 0.0)]
    return None if is_identity(out) else out


def is_identity(t: dict | None) -> bool:
    if not t:
        return True
    return (all(t.get(k, n) == n for k, n in NEUTRAL.items())
            and not any((t.get("crop") or {}).values())
            and not any(any(p) for p in (t.get("distort") or {}).values()))


def frame_quad(t: dict, w: int, h: int) -> list[tuple[float, float]]:
    """Where the frame's corners land, in frame pixels with y down, in the order tl, tr, br, bl."""
    sx, sy = t["scale"] * t["scaleX"], t["scale"] * t["scaleY"]
    a = math.radians(t["rotation"])
    cos, sin = math.cos(a), math.sin(a)
    cx, cy = w / 2, h / 2
    ax, ay = t["anchorX"] * w, -t["anchorY"] * h
    px, py = t["x"] * w, -t["y"] * h
    base = {"tl": (0, 0), "tr": (w, 0), "br": (w, h), "bl": (0, h)}

    def place(x: float, y: float) -> tuple[float, float]:
        vx, vy = (x - cx - ax) * sx, (y - cy - ay) * sy
        return (cx + px + vx * cos + vy * sin, cy + py - vx * sin + vy * cos)

    d = t["distort"]
    return [place(base[k][0] + d[k][0] * w, base[k][1] - d[k][1] * h) for k in CORNERS]


def homography(quad: list[tuple[float, float]]) -> tuple[float, ...]:
    """(a, b, c, d, e, f, g, h) taking (u, v) of the unit square to the quad (tl, tr, br, bl):
    x = (a u + b v + c) / z, y = (d u + e v + f) / z, z = g u + h v + 1 (Heckbert's square-to-quad)."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = quad
    dx1, dy1, dx2, dy2 = x1 - x2, y1 - y2, x3 - x2, y3 - y2
    sx, sy = x0 - x1 + x2 - x3, y0 - y1 + y2 - y3
    den = dx1 * dy2 - dx2 * dy1
    if (abs(sx) < 1e-9 and abs(sy) < 1e-9) or abs(den) < 1e-9:
        g = h = 0.0
    else:
        g = (sx * dy2 - dx2 * sy) / den
        h = (dx1 * sy - sx * dy1) / den
    return (x1 - x0 + g * x1, x3 - x0 + h * x3, x0, y1 - y0 + g * y1, y3 - y0 + h * y3, y0, g, h)


def apply(m: tuple[float, ...], u: float, v: float) -> tuple[float, float]:
    a, b, c, d, e, f, g, h = m
    z = g * u + h * v + 1
    return ((a * u + b * v + c) / z, (d * u + e * v + f) / z)


def _even(v: float) -> int:
    return int(round(v / 2)) * 2


def transform_filter(t: dict | None, w: int, h: int) -> str:
    """ffmpeg filters for a cleaned transform on a w x h yuv420p (tv range) clip; "" when it changes nothing.

    The picture is trimmed to black, shrunk with lanczos first when it ends up smaller (perspective samples
    bilinearly), padded with black so its edges blend into black, placed with perspective and cut back to the
    frame. Opacity scales luma and chroma toward black, the same as the preview's element over black."""
    if is_identity(t):
        return ""
    parts = []
    c = t["crop"]
    left, right, top, bottom = (_even(c["l"] * w), _even(c["r"] * w), _even(c["t"] * h), _even(c["b"] * h))
    if left or right or top or bottom:
        parts.append(f"crop={w - left - right}:{h - top - bottom}:{left}:{top},pad={w}:{h}:{left}:{top}:color=black")
    quad = frame_quad(t, w, h)
    base = [(0, 0), (w, 0), (w, h), (0, h)]
    if any(abs(q[0] - b[0]) > 1e-3 or abs(q[1] - b[1]) > 1e-3 for q, b in zip(quad, base)):
        fx = min(1.0, max(math.dist(quad[0], quad[1]), math.dist(quad[3], quad[2])) / w)
        fy = min(1.0, max(math.dist(quad[0], quad[3]), math.dist(quad[1], quad[2])) / h)
        sw, sh = max(2, _even(w * fx)), max(2, _even(h * fy))
        if (sw, sh) != (w, h):
            parts.append(f"scale={sw}:{sh}:flags=lanczos")
        parts.append(f"pad={w + 2 * PAD}:{h + 2 * PAD}:{PAD}:{PAD}:color=black")
        m = homography(quad)
        # perspective's corners are the padded input's tl, tr, bl, br, in units of the (shrunk) picture
        lo_u, hi_u, lo_v, hi_v = -PAD / sw, (w + PAD) / sw, -PAD / sh, (h + PAD) / sh
        pts = [apply(m, lo_u, lo_v), apply(m, hi_u, lo_v), apply(m, lo_u, hi_v), apply(m, hi_u, hi_v)]
        coords = ":".join(f"x{i}={x + PAD:.3f}:y{i}={y + PAD:.3f}" for i, (x, y) in enumerate(pts))
        parts.append(f"perspective={coords}:sense=destination,crop={w}:{h}:{PAD}:{PAD}")
    o = t["opacity"]
    if o < 1:
        parts.append(f"lutyuv=y=16+(val-16)*{o:.4f}:u=128+(val-128)*{o:.4f}:v=128+(val-128)*{o:.4f}")
    return ",".join(parts)
