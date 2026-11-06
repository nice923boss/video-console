"""Putting library media and clips on the timeline, for the server twins in twins.py: free stretches on a track
(lib/track-fit.js), framing a picture (lib/sequence.js), placing media (lib/media-actions.js) and moving a shot
to an upper track and back (lib/lift.js). Each follows its JS source so the same project gives the same ops."""
from __future__ import annotations

import math

from . import edits, opfields
from .jscompat import clamp, js_number, jsround, r3, stem
from .sequence import VOICE_TRACK, audio_track, build_sequence, clip_speed
from .takes import new_media
from .util import AppError, new_id, now

EPS = 1e-3


# ---------------------------------------------------------------- lib/track-fit.js
def fit_start(want: float, length: float, taken: list[dict]) -> float:
    """Where a clip `length` long, wanted at `want`, fits without overlapping any of `taken` ([{s, e}])."""
    spans = sorted(taken, key=lambda sp: sp["s"])
    best, dist, frm = max(0, want), math.inf, 0
    for sp in [*spans, {"s": math.inf, "e": math.inf}]:
        if sp["s"] - frm >= length - EPS:
            t = min(max(want, frm), sp["s"] - length)
            if abs(t - want) < dist:
                dist, best = abs(t - want), t
        frm = max(frm, sp["e"])
    return best


def fit_gap(want: float, length: float, taken: list[dict]) -> float | None:
    """Where a clip moved over from another track lands: inside the free stretch under its middle, or None."""
    mid = max(0, want) + length / 2
    if any(mid > sp["s"] + EPS and mid < sp["e"] - EPS for sp in taken):
        return None
    frm = max([0, *(sp["e"] for sp in taken if sp["e"] <= mid + EPS)])
    to = min([math.inf, *(sp["s"] for sp in taken if sp["s"] >= mid - EPS)])
    if to - frm < length - EPS:
        return None
    return min(max(want, frm), to - length)


def layer_spans(overlays: list[dict] | None, layer: str, skip: str | None = None) -> list[dict]:
    """The overlays on one picture layer as spans, leaving out `skip`."""
    out = []
    for o in overlays or []:
        if o.get("id") != skip and o.get("layer") == layer:
            s = float(o.get("start") or 0)
            out.append({"s": s, "e": s + float(o.get("length") or 0)})
    return out


# ---------------------------------------------------------------- lib/sequence.js framing
def _aspect(media: dict) -> float:
    w, h = media.get("width"), media.get("height")
    return w / h if w and h else 16 / 9


def fit_frame(media: dict, frame_w: float, frame_h: float) -> dict:
    """fitFrame: the largest size that shows the whole picture, centered."""
    full = frame_w / frame_h / _aspect(media)
    w = 1 / full if full > 1 else 1
    h = min(1, full)
    return {"x": jsround((1 - w) / 2, 4), "y": jsround((1 - h) / 2, 4), "w": jsround(w, 4)}


def fill_frame(media: dict, frame_w: float, frame_h: float) -> dict:
    """fillFrame: the smallest size that covers the whole frame, centered."""
    full = frame_w / frame_h / _aspect(media)
    w = min(4, 1 / full) if full < 1 else 1
    h = full * w
    return {"x": jsround((1 - w) / 2, 4), "y": jsround(max(-4, (1 - h) / 2), 4), "w": jsround(w, 4)}


def new_overlay(oid: str, media: dict, layer: str, start: float, frame_w: float, frame_h: float) -> dict:
    """newOverlay: a clip on an upper track that fills the frame."""
    video = media.get("kind") == "video" and media.get("duration")
    return {"id": oid, "mediaId": media["id"], "layer": layer, "name": str(media.get("name") or "")[:120],
            "start": r3(max(0, start)), "in": 0,
            "length": r3(max(0.1, float(media["duration"]))) if video else 5,
            **fit_frame(media, frame_w, frame_h), "opacity": 1, "volume": 1}


# ---------------------------------------------------------------- lib/media-actions.js
TAKE_KEYS = ("kind", "file", "thumb", "strip", "wave", "width", "height", "duration", "fps", "hasAudio")


def take_from_media(media: dict, shot_id: str) -> dict:
    """takeFromMedia: a take that points at the same files as the media item."""
    return {"id": new_id("t"), "shot": shot_id, **{k: media.get(k) for k in TAKE_KEYS}, "created": now(),
            "source": {"backend": "import", "name": media.get("name") or ""}, "prompt": "", "negative": "",
            "seed": None, "params": {}, "parent": None, "favorite": False, "rating": 0, "note": "",
            "label": stem(media.get("name")), "jobId": None}


def _shot_ops(media: dict, index: int) -> list[dict]:
    """shotOps: a new shot at `index` whose picture is the media item."""
    shot = {"id": new_id("s"), "name": stem(media.get("name"))}
    if media.get("kind") == "video" and media.get("duration"):
        shot = {**shot, "duration": r3(clamp(float(media["duration"]), 0.2, 600))}
    take = take_from_media(media, shot["id"])
    key = "activeImage" if media.get("kind") == "image" else "activeVideo"
    return [{"op": "shot.add", "shot": shot, "index": index}, {"op": "take.add", "take": take},
            {"op": "shot.update", "id": shot["id"], "patch": {key: take["id"]}}]


def _busy(audio: list[dict], project: dict, key: str, start: float, length: float) -> bool:
    """busy: whether a sound at `start` would overlap another clip on music track `key` (voice rows never do)."""
    if key == VOICE_TRACK:
        return False
    return any(audio_track(a) == key
               and start < float(a.get("start") or 0) + edits.audio_length(project, a) - 1e-3
               and float(a.get("start") or 0) < start + length - 1e-3 for a in audio)


def place_ops(project: dict, media_list: list[dict], index: int, start: float, layer: str | None = None,
              track: str | None = None) -> list[dict]:
    """placeMedia: pictures and clips as new shots from `index` on, or on upper track `layer` ("new": a new
    track each) in the nearest free stretch; sounds at `start` on `track` where it is free, else their own track."""
    out: list[dict] = []
    audio = list(project.get("audio") or [])
    overlays = list(project.get("overlays") or [])
    width, height = project["settings"]["width"], project["settings"]["height"]
    at = index
    for media in media_list:
        visual = media.get("kind") in ("image", "video")
        if visual and layer:
            lid = layer
            if layer == "new":
                lid = new_id("l")
                out.append({"op": "layer.add", "layer": {"id": lid}})
            placed = new_overlay(new_id("o"), media, lid, start, width, height)
            item = placed if layer == "new" else {
                **placed, "start": r3(fit_start(placed["start"], placed["length"], layer_spans(overlays, lid)))}
            overlays.append(item)
            out.append({"op": "overlay.add", "item": item})
        elif visual:
            out.extend(_shot_ops(media, at))
            at += 1
        elif media.get("kind") == "audio":
            item = {"id": new_id("a"), "mediaId": media["id"], "name": str(media.get("name") or "")[:120],
                    "start": r3(start), "volume": 1 if media.get("gain") is None else media["gain"]}
            if track and not _busy(audio, project, track, item["start"], max(0.1, float(media.get("duration") or 0))):
                item = {**item, "track": track}
            audio.append(item)
            out.append({"op": "audio.add", "item": item})
    return out


def shot_index_at(project: dict, clips: list[dict], t: float) -> int:
    """shotIndexAt in components/timeline.js: the shot index a picture dropped at t goes in at."""
    k = sum(1 for c in clips if c["start"] + c["length"] / 2 < t)
    if k < len(clips):
        return clips[k]["index"]
    return clips[-1]["index"] + 1 if clips else len(project["shots"])


# ---------------------------------------------------------------- lib/lift.js
def _drop_sound(project: dict, clip: dict) -> dict | None:
    """The one sound detached under the shot, still on the shot's piece of the file as a drop left it."""
    sounds = [a for a in project.get("audio") or [] if a.get("shotId") == clip["shot"]["id"]]
    if len(sounds) != 1:
        return None
    a = sounds[0]
    near = all(abs(js_number(a, k) - float(clip[k])) < 1e-3 for k in ("start", "in", "out"))
    return a if a.get("takeId") == clip["take"]["id"] and near else None


def lift_ops(project: dict, clip: dict, target: str, start: float) -> dict:
    """liftOps: {ops, id} that turn the shot into a clip on upper track target ("layer:<id>" or "new-layer")
    and leave an empty shot of the same length and transition in its place; a sped shot keeps its speed."""
    shot, take = clip["shot"], clip["take"]
    if not take:
        raise AppError("這個鏡頭還沒有畫面，沒有東西可以移到上層軌")
    sound = _drop_sound(project, clip) if shot.get("audioDetached") else None
    if shot.get("audioDetached") and not sound:
        raise AppError("已分離原聲的鏡頭要先還原原聲，才能移到上層軌")
    ops: list[dict] = [{"op": "audio.delete", "id": sound["id"]}] if sound else []
    media = next((m for m in (project.get("media") or {}).values()
                  if m.get("file") == take.get("file") and m.get("kind") == take.get("kind")), None)
    if not media:
        media = new_media(take, take.get("label") or shot.get("name") or "", new_id("m"))
        ops.append({"op": "media.add", "media": media})
    layer = target[6:]
    if target == "new-layer":
        layer = new_id("l")
        ops.append({"op": "layer.add", "layer": {"id": layer}})
    settings = project["settings"]
    frame = fill_frame if settings.get("fit") == "cover" else fit_frame
    oid = new_id("o")
    if sound:
        volume = {"volume": 1 if sound.get("volume") is None else sound["volume"]}
    else:
        volume = {"volume": shot["volume"]} if shot.get("volume") is not None else {}
    item = {"id": oid, "mediaId": media["id"], "layer": layer, "name": shot.get("name") or media.get("name") or "",
            "start": r3(max(0, start)), "in": r3(clip["in"]) if clip["source"] == "video" else 0,
            "length": r3(clip["length"]), **({"speed": clip["speed"]} if clip["speed"] != 1 else {}),
            **frame(media, float(settings["width"]), float(settings["height"])),
            "opacity": 1, "volume": 1, **(shot.get("pip") or {}), **volume,
            **({"disabled": True} if shot.get("disabled") else {})}
    gap = {"id": new_id("s"), "name": "空白", "duration": r3(max(0.2, clip["length"])),
           **({"transition": shot["transition"]} if "transition" in shot else {})}
    ops += [{"op": "overlay.add", "item": item}, {"op": "shot.delete", "id": shot["id"]},
            {"op": "shot.add", "shot": gap, "index": clip["index"]}]
    return {"ops": ops, "id": oid}


def drop_ops(project: dict, item: dict, media: dict, index: int, take: dict) -> dict:
    """dropOps: {ops, shot} that turn the upper clip into a shot at `index` with the same piece of the file;
    its place, size, opacity and keying wait in shot.pip, a volume other than 100% on a detached sound."""
    video = media.get("kind") == "video"
    length = float(item.get("length") or 0.1)
    speed = clip_speed(item, "video" if video else "image")
    t_in = r3(float(item.get("in") or 0))
    out = r3(t_in + length * speed)
    end = float(media.get("duration") or 0)
    pip = {k: item[k] for k in opfields.PIP_KEYS if item.get(k) is not None}
    shot = {"id": take["shot"], "name": stem(str(item.get("name") or media.get("name") or "")),
            "duration": jsround(clamp(length, 0.2, 600), 3 if video else 2),
            **({"trimIn": t_in, "trimOut": None if end and out >= end - 1e-3 else out} if video else {}),
            **({"speed": speed} if speed != 1 else {}), **({"pip": pip} if pip else {})}
    pick = {"activeVideo" if video else "activeImage": take["id"]}
    ops = [{"op": "shot.add", "shot": shot, "index": index}, {"op": "take.add", "take": take},
           {"op": "shot.update", "id": shot["id"], "patch": pick}]
    volume = 1 if item.get("volume") is None else item["volume"]
    if video and media.get("hasAudio") and abs(float(volume) - 1) > 1e-4:
        shots = list(project["shots"])
        shots.insert(index, {**shot, **pick})
        trial = {**project, "shots": shots, "takes": {**project["takes"], take["id"]: take}}
        clip = next((c for c in build_sequence(trial) if c["shot"]["id"] == shot["id"]), None)
        if clip:
            ops += [{"op": "shot.update", "id": shot["id"], "patch": {"audioDetached": True}},
                    {"op": "audio.add", "item": {
                        "id": new_id("a"), "takeId": take["id"], "shotId": shot["id"], "start": r3(clip["start"]),
                        "in": r3(clip["in"]), "out": r3(clip["out"]), "name": f"{shot['name'] or '鏡頭'} 原聲",
                        **({"speed": speed} if speed != 1 else {}), "volume": volume}}]
    ops.append({"op": "overlay.delete", "id": item["id"]})
    return {"ops": ops, "shot": shot}
