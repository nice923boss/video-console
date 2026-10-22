"""Linked edits: captions, voice-overs, soundtrack items and upper-track clips follow the shot they sit on.

Before a batch of shot edits each item is pinned to the clip under its start (the later clip inside a
transition overlap), at a point in that clip's source. Afterwards the item moves by however far that
point moved. Items on a shot that left the sequence keep their offset from where that shot used to
begin. Items the batch itself changed are left alone.
"""
from __future__ import annotations

from .sequence import build_sequence

EPS = 1e-4


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
    for key in ("captions", "audio", "overlays", "adjustments"):
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
        # A point trimmed off the head of its clip sticks to the clip's new start.
        moved = max(0.0, old["in"] + local - new["in"])
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


def _inside_deleted(old_clips: list[dict], deleted: set[str], start: float, end: float) -> bool:
    """True when the span start..end sat wholly on a clip whose shot was deleted."""
    i = _clip_at(old_clips, start)
    if i < 0 or old_clips[i]["shot"]["id"] not in deleted:
        return False
    return end <= old_clips[i]["start"] + old_clips[i]["length"] + EPS


def follow_ops(before: dict, after: dict) -> list[dict]:
    """Update ops that move the items of `after` to follow its shots, compared with `before`.

    Captions lying wholly on a deleted shot go with it (and so do their voice-overs), the way a ripple
    delete clears the linked tracks; a shot only taken out of the sequence keeps them.
    """
    old_clips = build_sequence(before)
    if not old_clips:
        return []
    new_clips = build_sequence(after)
    old_starts = _starts(before)
    deleted = {s["id"] for s in before.get("shots") or []} - {s["id"] for s in after.get("shots") or []}
    ops: list[dict] = []
    caption_shift: dict[str, float] = {}
    dropped: set[str] = set()

    for cap in after.get("captions") or []:
        start = float(cap.get("start") or 0.0)
        if old_starts.get(("captions", cap["id"])) != start:
            continue
        if deleted and _inside_deleted(old_clips, deleted, start, float(cap.get("end") or 0.0)):
            dropped.add(cap["id"])
            ops.append({"op": "caption.delete", "id": cap["id"]})
            continue
        d = _shift(old_clips, new_clips, start)
        caption_shift[cap["id"]] = d
        if abs(d) <= EPS:
            continue
        new_start = round(max(0.0, start + d), 3)
        length = float(cap.get("end") or 0.0) - start
        ops.append({"op": "caption.update", "id": cap["id"],
                    "patch": {"start": new_start, "end": round(new_start + length, 3)}})

    for key, name in (("audio", "audio"), ("overlays", "overlay"), ("adjustments", "adjust")):
        for item in after.get(key) or []:
            start = float(item.get("start") or 0.0)
            if old_starts.get((key, item["id"])) != start:
                continue
            # A voice-over keeps in step with its caption.
            cid = item.get("captionId") if key == "audio" else None
            if cid in dropped:
                ops.append({"op": "audio.delete", "id": item["id"]})
                continue
            d = caption_shift[cid] if cid in caption_shift else _shift(old_clips, new_clips, start)
            if abs(d) > EPS:
                ops.append({"op": f"{name}.update", "id": item["id"], "patch": {"start": round(max(0.0, start + d), 3)}})
    return ops
