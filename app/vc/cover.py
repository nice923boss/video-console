"""Where upper-track pictures hide the whole frame: no slate is burnt in under them, and the editor offers to
hide the picture of a shot they cover from end to end (its sound stays).

A clip hides the frame when it is an opaque picture (not a see-through title card, keyed, faded or keyframed)
that reaches every edge of the frame, on a shown track.
"""
from __future__ import annotations

from .sequence import build_sequence

EDGE = 0.002  # fraction of the frame an edge may fall short and still count as reaching it
OPAQUE_STILLS = (".jpg", ".jpeg")  # stills that cannot be see-through


def _hides_frame(item: dict, media: dict | None, frame_aspect: float) -> bool:
    if not media or item.get("disabled"):
        return False
    if media.get("kind") == "video":
        if media.get("alphaCodec"):
            return False
    elif media.get("kind") != "image" or not str(media.get("file") or "").lower().endswith(OPAQUE_STILLS):
        return False
    if (item.get("chroma") or {}).get("on") or float(item.get("opacity", 1.0)) < 1:
        return False
    if any(k in kf for kf in item.get("keyframes") or [] for k in ("x", "y", "w", "opacity")):
        return False
    mw, mh = float(media.get("width") or 0), float(media.get("height") or 0)
    if mw <= 0 or mh <= 0:
        return False
    x, y, w = float(item.get("x") or 0.0), float(item.get("y") or 0.0), float(item.get("w") or 0.35)
    h = w * frame_aspect * mh / mw  # as a fraction of the frame height
    return x <= EDGE and y <= EDGE and x + w >= 1 - EDGE and y + h >= 1 - EDGE


def covered_spans(project: dict) -> list[tuple[float, float]]:
    """Merged (start, end) timeline spans where upper-track clips hide the whole frame."""
    s = project["settings"]
    aspect = float(s["width"]) / float(s["height"])
    shown = {ly.get("id") for ly in project.get("layers") or [] if not ly.get("hidden")}
    media = project.get("media") or {}
    spans = sorted(
        (float(o.get("start") or 0.0), float(o.get("start") or 0.0) + float(o.get("length") or 0.0))
        for o in project.get("overlays") or []
        if o.get("layer") in shown and _hides_frame(o, media.get(o.get("mediaId") or ""), aspect)
    )
    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.001:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def uncovered(start: float, end: float, spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of [start, end) that the spans leave open."""
    out = []
    for a, b in spans:
        if b <= start or a >= end:
            continue
        if a > start:
            out.append((start, a))
        start = max(start, b)
    if end > start:
        out.append((start, end))
    return out


def covered_shots(project: dict) -> set[str]:
    """Ids of the sequence's shown shots that upper-track clips hide from end to end, give or take half a frame."""
    spans = covered_spans(project)
    if not spans:
        return set()
    half = 0.5 / float(project["settings"]["fps"])
    return {
        clip["shot"]["id"] for clip in build_sequence(project)
        if not clip["shot"].get("disabled") and not clip["shot"].get("pictureHidden")
        and all(b - a <= half for a, b in uncovered(clip["start"], clip["start"] + clip["length"], spans))
    }


def newly_covered(before: dict, after: dict) -> list[str]:
    """Shots with a picture an edit has just left hidden end to end, in shot-list order (the editor asks about
    these). A shot with no picture yet is black under the clip anyway, as a lifted shot's empty place is."""
    pictured = {c["shot"]["id"] for c in build_sequence(after) if c["source"] in ("video", "image")}
    new = (covered_shots(after) & pictured) - covered_shots(before)
    return [s["id"] for s in after.get("shots") or [] if s["id"] in new]
