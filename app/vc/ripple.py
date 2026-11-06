"""Ripple delete of a clip above the picture lane (Shift+Delete), mirror of web/js/lib/ripple.js: the clip goes and
what comes after it on the same track moves back by its length. A caption and its voice-over count as one."""
from __future__ import annotations

from typing import Callable

from .jscompat import r3
from .sequence import VOICE_TRACK, audio_track

LISTS = {"caption": "captions", "audio": "audio", "overlay": "overlays", "adjust": "adjustments"}


def _audio_length(item: dict, media: dict | None) -> float:
    """audioLength with the library item only: ripple.js passes project.media[item.mediaId], so a sound detached
    from a shot counts as 0.1 s long unless it has an out point."""
    full = float(media["duration"]) if media and media.get("duration") else 0.0
    t_out = full if item.get("out") is None else float(item["out"])
    return max(0.1, (t_out - float(item.get("in") or 0)) / float(item.get("speed") or 1))


def _span(project: dict, kind: str, item: dict) -> tuple[float, float]:
    start = float(item.get("start") or 0)
    if kind == "caption":
        return start, float(item.get("end") or 0)
    if kind == "audio":
        return start, start + _audio_length(item, (project.get("media") or {}).get(item.get("mediaId")))
    return start, start + float(item.get("length") or 0)


def _partners(project: dict, kind: str, item: dict) -> list[tuple[str, dict]]:
    """A caption's voice-overs, or a voice-over's caption."""
    if kind == "caption":
        return [("audio", a) for a in project.get("audio") or [] if a.get("captionId") == item["id"]]
    if kind == "audio" and item.get("captionId"):
        return [("caption", c) for c in project.get("captions") or [] if c.get("id") == item["captionId"]]
    return []


def ripple_ops(project: dict, kind: str, item_id: str) -> list[dict] | None:
    """rippleOps: the ops for ripple-deleting clip `item_id` of type `kind`, or None when there is no such clip."""
    key = LISTS.get(kind)
    item = next((x for x in project.get(key) or [] if x.get("id") == item_id), None) if key else None
    if not item:
        return None
    start, end = _span(project, kind, item)
    same_track: Callable[[dict], bool] = {
        "caption": lambda x: True,
        "audio": lambda x: audio_track(x) == audio_track(item),
        "overlay": lambda x: x.get("layer") == item.get("layer"),
        "adjust": lambda x: True,
    }[kind]
    gone = [(kind, item), *_partners(project, kind, item)]
    out = [{"op": f"{t}.delete", "id": x["id"]} for t, x in gone]
    done = {x["id"] for _, x in gone}

    def shift(t: str, x: dict, d: float) -> None:
        if x["id"] in done:
            return
        done.add(x["id"])
        s = r3(float(x.get("start") or 0) + d)
        patch = {"start": s, "end": r3(s + float(x.get("end") or 0) - float(x.get("start") or 0))} \
            if t == "caption" else {"start": s}
        out.append({"op": f"{t}.update", "id": x["id"], "patch": patch})

    later = [x for x in project.get(key) or []
             if x["id"] not in done and float(x.get("start") or 0) >= start - 1e-6 and same_track(x)]
    for x in later:
        s = float(x.get("start") or 0)
        d = max(start, s - (end - start)) - s
        if abs(d) < 1e-6:
            continue
        shift(kind, x, d)
        if kind == "caption" or audio_track(x) == VOICE_TRACK:
            for t, y in _partners(project, kind, x):
                shift(t, y, d)
    return out
