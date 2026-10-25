"""Linked edits: captions, voice-overs, soundtrack items and upper-track clips follow the shot they sit on.

Before a batch of shot edits each item is pinned to the clip under its start (the later clip inside a
transition overlap), at a point in that clip's source. Afterwards the item moves by however far that
point moved. Items on a deleted shot keep their offset from where that shot used to begin. Items the
batch itself changed, and soundtrack clips fixed to the timeline, are left alone.

A shot taken out of the sequence takes the items lying wholly on it along: they leave the project's
lists and wait in the shot's "parked" field, timed from the shot's start, until it comes back.
"""
from __future__ import annotations

from .edits import audio_length
from .sequence import build_sequence

EPS = 1e-4
LISTS = (("captions", "caption"), ("audio", "audio"), ("overlays", "overlay"), ("adjustments", "adjust"))


def _clip_at(clips: list[dict], t: float) -> int:
    idx = -1
    for i, clip in enumerate(clips):
        if clip["start"] <= t + 1e-6:
            idx = i
        else:
            break
    return idx


def _starts(project: dict) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for key, _ in LISTS:
        for item in project.get(key) or []:
            out[(key, item["id"])] = float(item.get("start") or 0.0)
    return out


def _shift(old_clips: list[dict], new_clips: list[dict], t: float) -> float:
    """How far time t (on the old sequence) moves on the new one."""
    i = _clip_at(old_clips, t)
    if i < 0:
        return 0.0
    old = old_clips[i]
    local = t - old["start"]
    new_by_id = {c["shot"]["id"]: c for c in new_clips}
    new = new_by_id.get(old["shot"]["id"])
    if new is not None:
        # A point trimmed off the head of its clip sticks to the clip's new start. Points follow the
        # source frame they sat on, so a speed change stretches them along with the clip.
        source = old["in"] + local * old.get("speed", 1.0)
        moved = max(0.0, source - new["in"]) / new.get("speed", 1.0)
        return new["start"] + moved - t
    # The shot is gone: land where it used to begin, which is now the next remaining clip.
    nxt = next((new_by_id[c["shot"]["id"]] for c in old_clips[i + 1:] if c["shot"]["id"] in new_by_id), None)
    if nxt is not None:
        gap = nxt["start"]
    elif new_clips:
        gap = new_clips[-1]["start"] + new_clips[-1]["length"]
    else:
        gap = 0.0
    return gap + local - t


def _owner(old_clips: list[dict], shots: set[str], start: float, end: float) -> dict | None:
    """The clip of one of `shots` that the span start..end sat wholly on, if any."""
    i = _clip_at(old_clips, start)
    if i < 0 or old_clips[i]["shot"]["id"] not in shots:
        return None
    return old_clips[i] if end <= old_clips[i]["start"] + old_clips[i]["length"] + EPS else None


def _end(project: dict, key: str, item: dict) -> float:
    start = float(item.get("start") or 0.0)
    if key == "captions":
        return float(item.get("end") or 0.0)
    if key == "audio":
        return start + audio_length(project, item)
    return start + float(item.get("length") or 0.0)


def _moved(item: dict, d: float) -> dict:
    out = {**item, "start": round(max(0.0, float(item.get("start") or 0.0) + d), 3)}
    if "end" in item:
        out["end"] = round(out["start"] + float(item["end"]) - float(item.get("start") or 0.0), 3)
    return out


def _restore_ops(project: dict, clips: list[dict]) -> list[dict]:
    """Put back what shots now in the sequence parked, at their new start; items whose file or track was
    deleted meanwhile stay out."""
    ops: list[dict] = []
    layers = {layer["id"] for layer in project.get("layers") or []}
    caption_tracks = {track["id"] for track in project.get("captionTracks") or []}
    for clip in clips:
        parked = clip["shot"].get("parked")
        if not parked:
            continue
        for key, name in LISTS:
            for item in parked.get(key) or []:
                if key == "audio" and item.get("mediaId") not in project["media"] \
                        and item.get("takeId") not in project["takes"]:
                    continue
                if key == "overlays" and (item.get("mediaId") not in project["media"] or item.get("layer") not in layers):
                    continue
                if key == "captions" and item.get("track") and item["track"] not in caption_tracks:
                    item = {**item, "track": None}  # its row was deleted meanwhile: back on the main row
                ops.append({"op": f"{name}.add", "item": _moved(item, clip["start"])})
        ops.append({"op": "shot.update", "id": clip["shot"]["id"], "patch": {}, "unset": ["parked"]})
    return ops


def follow_ops(before: dict, after: dict, shift: bool = True) -> list[dict]:
    """Update ops that move the items of `after` to follow its shots, compared with `before`.

    Captions lying wholly on a deleted shot go with it (and so do their voice-overs), the way a ripple
    delete clears the linked tracks. A shot taken out of the sequence parks the items lying wholly on
    it, and puts them back when it returns. With shift False only that putting back happens.
    """
    new_clips = build_sequence(after)
    ops = _restore_ops(after, new_clips)
    old_clips = build_sequence(before)
    if not old_clips or not shift:
        return ops
    old_starts = _starts(before)
    deleted = {s["id"] for s in before.get("shots") or []} - {s["id"] for s in after.get("shots") or []}
    taken_out = {c["shot"]["id"] for c in old_clips} - deleted - {c["shot"]["id"] for c in new_clips}
    caption_shift: dict[str, float] = {}
    dropped: set[str] = set()
    parked: dict[str, dict[str, list[dict]]] = {}  # shot id -> list key -> items timed from the shot's start
    parked_caps: dict[str, str] = {}  # caption id -> shot id

    def park(clip: dict, key: str, name: str, item: dict) -> None:
        parked.setdefault(clip["shot"]["id"], {}).setdefault(key, []).append(_moved(item, -clip["start"]))
        ops.append({"op": f"{name}.delete", "id": item["id"]})

    for cap in after.get("captions") or []:
        start = float(cap.get("start") or 0.0)
        if old_starts.get(("captions", cap["id"])) != start:
            continue
        end = float(cap.get("end") or 0.0)
        if deleted and _owner(old_clips, deleted, start, end):
            dropped.add(cap["id"])
            ops.append({"op": "caption.delete", "id": cap["id"]})
            continue
        clip = _owner(old_clips, taken_out, start, end) if taken_out else None
        if clip:
            parked_caps[cap["id"]] = clip["shot"]["id"]
            park(clip, "captions", "caption", cap)
            continue
        d = _shift(old_clips, new_clips, start)
        caption_shift[cap["id"]] = d
        if abs(d) <= EPS:
            continue
        new_start = round(max(0.0, start + d), 3)
        ops.append({"op": "caption.update", "id": cap["id"],
                    "patch": {"start": new_start, "end": round(new_start + end - start, 3)}})

    by_shot = {c["shot"]["id"]: c for c in old_clips}
    for key, name in LISTS[1:]:
        for item in after.get(key) or []:
            start = float(item.get("start") or 0.0)
            if old_starts.get((key, item["id"])) != start or item.get("fixed"):
                continue
            # A voice-over keeps in step with its caption.
            cid = item.get("captionId") if key == "audio" else None
            if cid in dropped:
                ops.append({"op": "audio.delete", "id": item["id"]})
                continue
            clip = by_shot[parked_caps[cid]] if cid in parked_caps else None
            if clip is None and taken_out and cid not in caption_shift:
                clip = _owner(old_clips, taken_out, start, _end(after, key, item))
            if clip:
                park(clip, key, name, item)
                continue
            d = caption_shift[cid] if cid in caption_shift else _shift(old_clips, new_clips, start)
            if abs(d) > EPS:
                ops.append({"op": f"{name}.update", "id": item["id"], "patch": {"start": round(max(0.0, start + d), 3)}})
    for sid, lists in parked.items():
        ops.append({"op": "shot.update", "id": sid, "patch": {"parked": lists}})
    return ops
