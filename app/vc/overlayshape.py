"""How an upper-track clip's crop and rotation become ffmpeg filters (export.py _plan_overlays); the preview
mirrors it in app/web/js/lib/sequence.js overlayShape."""
from __future__ import annotations

import math

from .keyframes import expr, points

PLACE_KEYS = ("x", "y", "w", "rotation")  # what the full-frame switch takes over


def framed(item: dict, media: dict | None, w: float, h: float) -> dict:
    """The clip as it plays: with its full-frame switch on ("full"), the whole picture fitted to the frame,
    centered, uncropped and upright, whatever place, size, crop, rotation and keyframes of those it keeps for
    when the switch is off; opacity, volume and keying stay. Mirrors framed in app/web/js/lib/sequence.js."""
    if not item.get("full") or not media:
        return item
    aspect = media["width"] / media["height"] if media.get("width") and media.get("height") else 16 / 9
    full = w / h / aspect
    fw = 1 / full if full > 1 else 1.0
    fh = min(1.0, full)
    kfs = [k for k in ({f: v for f, v in kf.items() if f not in PLACE_KEYS} for kf in item.get("keyframes") or [])
           if set(k) - {"t"}]
    out = {k: v for k, v in item.items() if k not in ("crop", "keyframes")}
    return {**out, "x": round((1 - fw) / 2, 4), "y": round((1 - fh) / 2, 4), "w": round(fw, 4), "rotation": 0.0,
            **({"keyframes": kfs} if kfs else {})}


def shape_overlay(item: dict, media: dict, w: int, h: int, start: float,
                  keyed: set[str]) -> tuple[list[str], str | int, str | int] | None:
    """The filters that trim (crop) and turn (rotation) an upper-track clip and size it, with its overlay x and y.
    None when it is neither trimmed nor turned, so the plain chain applies.

    The trimmed part is cut off before scaling, so the picture below shows there. The turn runs on the part left,
    about that part's center, with the corners it opens filled transparent (black@0; c=none paints nothing there,
    so a changing angle leaves earlier frames behind); a changing angle turns on a square canvas as wide as the
    part's diagonal, room for any angle. x, y and w still describe the uncropped box. The preview does the same
    with a clip-path and a CSS rotate (player-engine.js place)."""
    crop = item.get("crop") or {}
    angles = [float(v) for _, v in points(item, "rotation")] or [float(item.get("rotation") or 0.0)]
    turned = any(angles)
    if not crop and not turned:
        return None
    left, right, top, bottom = (float(crop.get(k) or 0.0) for k in ("l", "r", "t", "b"))
    cw, ch = 1 - left - right, 1 - top - bottom
    aspect = media["width"] / media["height"] if media.get("width") and media.get("height") else 16 / 9
    tall = ch / (cw * aspect)  # the part left's height over its width
    chain = [f"crop=w='iw*{cw:.4f}':h='ih*{ch:.4f}':x='iw*{left:.4f}':y='ih*{top:.4f}':exact=1"] if crop else []
    grow = 1.0  # the turned picture's width over the part left's width
    if len(set(angles)) > 1:
        a = f"-({expr(item, 'rotation', 't', start)})*PI/180"  # ffmpeg turns clockwise
        chain += ["format=rgba", f"rotate=a='{a}':ow='ceil(hypot(iw,ih))':oh=ow:c=black@0"]
        grow = math.hypot(1, tall)
    elif turned:
        a = -math.radians(angles[0])
        chain += ["format=rgba", f"rotate=a={a:.6f}:ow='rotw({a:.6f})':oh='roth({a:.6f})':c=black@0"]
        grow = abs(math.cos(a)) + abs(math.sin(a)) * tall
    if "w" in keyed:
        chain += [f"scale=w='max(2,2*trunc({w * cw * grow:.4f}*({expr(item, 'w', 't', start, 0.35)})/2))':h=-2:"
                  "eval=frame:flags=lanczos", "setsar=1"]
    else:
        chain.append(f"scale={max(2, round(w * float(item.get('w') or 0.35) * cw * grow / 2) * 2)}:-2:flags=lanczos")
    # Where the part left starts (turned: its center) from the box's corner, in frame pixels per unit of w.
    fx = w * (left + (cw / 2 if turned else 0))
    fy = w * (top + (ch / 2 if turned else 0)) / aspect
    if turned or keyed & {"x", "y", "w"}:
        size = expr(item, "w", "t", start, 0.35)
        x = f"'{w}*({expr(item, 'x', 't', start)})+{fx:.4f}*({size}){'-overlay_w/2' if turned else ''}'"
        y = f"'{h}*({expr(item, 'y', 't', start)})+{fy:.4f}*({size}){'-overlay_h/2' if turned else ''}'"
        return chain, x, y
    size = float(item.get("w") or 0.35)
    return (chain, round(w * float(item.get("x") or 0.0) + fx * size),
            round(h * float(item.get("y") or 0.0) + fy * size))
