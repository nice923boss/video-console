"""The trim tool's edits (T on the edit page, web/js/components/tl-roll.js; mirror of lib/roll.js): roll, slip and
slide change the picture only. The other tracks stay put (link off), and the shots whose own sound plays have it
detached first (one row, as edits.detach), so the sound stays where it was. One call is one undo step."""
from __future__ import annotations

from .edits import MAX_STILL, MIN_STILL, MIN_VIDEO, _clips, detach, r3, track_busy
from .sequence import VOICE_TRACK, audio_track
from .util import AppError


def _seq_index(clips: list[dict], shot_id: str | None) -> int:
    i = next((k for k, c in enumerate(clips) if c["shot"]["id"] == shot_id), -1)
    if i < 0:
        raise AppError("找不到這個鏡頭，或它不在剪輯序列裡")
    return i


def _end_room(clip: dict) -> tuple[float, float]:
    """How far (timeline seconds) a clip's end can move earlier and later while its first frame stays."""
    if clip["source"] == "video":
        return clip["length"] - MIN_VIDEO / clip["speed"], (float(clip["take"]["duration"]) - clip["out"]) / clip["speed"]
    return clip["length"] - MIN_STILL, MAX_STILL - clip["length"]


def _head_room(clip: dict) -> tuple[float, float]:
    """How far a clip's first frame can move later and earlier while its last frame stays."""
    if clip["source"] == "video":
        return clip["length"] - MIN_VIDEO / clip["speed"], clip["in"] / clip["speed"]
    return clip["length"] - MIN_STILL, MAX_STILL - clip["length"]


def cut_range(before: dict, after: dict) -> tuple[float, float]:
    """How far the cut between two clips can move earlier (negative) and later."""
    return -max(0.0, min(_end_room(before)[0], _head_room(after)[1])), max(0.0, min(_end_room(before)[1], _head_room(after)[0]))


def slip_range(clip: dict) -> tuple[float, float]:
    """How far a video clip's footage can slip (timeline seconds, positive shows later footage)."""
    return -clip["in"] / clip["speed"], max(0.0, (float(clip["take"]["duration"]) - clip["out"]) / clip["speed"])


def _limit(by: float, span: tuple[float, float]) -> float:
    d = min(max(by, span[0]), span[1])
    if abs(d) < 1e-3:
        raise AppError("已經到底了：素材沒有多的畫面，或片段已經是最短")
    return d


def _end_patch(clip: dict, d: float) -> dict:
    if clip["source"] != "video":
        return {"duration": r3(clip["length"] + d)}
    out = clip["out"] + d * clip["speed"]
    return {"trimOut": None if out >= float(clip["take"]["duration"]) - 1e-3 else r3(out)}


def _head_patch(clip: dict, d: float) -> dict:
    if clip["source"] != "video":
        return {"duration": r3(clip["length"] - d)}
    return {"trimIn": r3(clip["in"] + d * clip["speed"])}


def _move_cut(before: dict, after: dict, by: float) -> tuple[list[dict], float]:
    d = _limit(by, cut_range(before, after))
    return [{"op": "shot.update", "id": before["shot"]["id"], "patch": _end_patch(before, d)},
            {"op": "shot.update", "id": after["shot"]["id"], "patch": _head_patch(after, d)}], r3(d)


def _keep_sound(project: dict, clips: list[dict]) -> list[dict]:
    """Detach ops for the clips that play their own sound, all on one row: a row of detached sound that is free
    there, else a new one."""
    adds, rest = [], []
    for clip in clips:
        if clip["source"] == "video" and clip["take"].get("hasAudio") and not clip["shot"].get("audioDetached"):
            add, *more = detach(project, clip["shot"]["id"])["ops"]
            adds.append(add["item"])
            rest += more
    if not adds:
        return []
    lo = min(a["start"] for a in adds)
    hi = max(a["start"] + (a["out"] - a["in"]) / a.get("speed", 1) for a in adds)
    rows = [k for k in dict.fromkeys(audio_track(a) for a in project.get("audio") or [] if a.get("shotId")) if k != VOICE_TRACK]
    row = next((k for k in rows if not track_busy(project, k, lo, hi - lo)), adds[0]["id"])
    return [*({"op": "audio.add", "item": {**a, **({"track": row} if row != a["id"] else {})}} for a in adds), *rest]


def _picture_only(project: dict, clips: list[dict], ops: list[dict], label: str, d: float) -> dict:
    return {"ops": [*_keep_sound(project, clips), *ops], "label": label, "by": d, "link": False}


def roll(project: dict, shot_id: str | None, by: float) -> dict:
    """Move the cut after a shot `by` seconds: it ends later and the next shot starts later in its footage (or
    both earlier), so the total length and everything past the two shots stay put."""
    clips = _clips(project)
    i = _seq_index(clips, shot_id)
    if i + 1 >= len(clips):
        raise AppError("這是最後一個鏡頭，後面沒有剪接點可以滾動")
    ops, d = _move_cut(clips[i], clips[i + 1], by)
    return _picture_only(project, clips[i:i + 2], ops, "滾動剪接點", d)


def slip(project: dict, shot_id: str | None, by: float) -> dict:
    """Show other footage in the same slot: in and out points both move `by` seconds (positive shows later
    footage). The clip keeps its place and length."""
    clips = _clips(project)
    clip = clips[_seq_index(clips, shot_id)]
    if clip["source"] != "video":
        raise AppError("滑動內容只能用在影片鏡頭")
    d = _limit(by, slip_range(clip))
    s = d * clip["speed"]
    out = clip["out"] + s
    patch = {"trimIn": r3(clip["in"] + s), "trimOut": None if out >= float(clip["take"]["duration"]) - 1e-3 else r3(out)}
    return _picture_only(project, [clip], [{"op": "shot.update", "id": clip["shot"]["id"], "patch": patch}], "滑動內容", r3(d))


def slide(project: dict, shot_id: str | None, by: float) -> dict:
    """Move a shot `by` seconds between its neighbours, unchanged: the one before ends later and the one after
    starts later in its footage (or both earlier), so the total length stays."""
    clips = _clips(project)
    i = _seq_index(clips, shot_id)
    if i == 0 or i + 1 >= len(clips):
        raise AppError("滑移鏡頭要前後都有鏡頭")
    ops, d = _move_cut(clips[i - 1], clips[i + 1], by)
    return _picture_only(project, clips[i - 1:i + 2], ops, "滑移鏡頭", d)
