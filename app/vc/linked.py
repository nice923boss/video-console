"""Linked edits: captions, voice-overs, soundtrack items and upper-track clips follow the shot they sit on.

Before a batch of shot edits each item is pinned to the clip under its start (the later clip inside a
transition overlap), at a point in that clip's source. Afterwards the item moves by however far that
point moved. A deleted shot takes the stretch of captions, detached sound, voice-track recordings, upper-track
picture and adjustment over it along (what runs on past it is cut there and closes up, so clips on one track
never end up overlapping). Voice-overs made from a caption's text go with their caption and are never cut.
Music and sound effects from the library are never cut in two: one lying wholly on deleted shots goes with
them, any other stays whole. Their keyframes follow the picture, so a dip under a voice stays under it: keys
over cut picture (a deleted shot, a trimmed-off part) drop out and the rest close up, keys after added picture
(a longer shot, an inserted one) move back while the added stretch holds the value where it went in. When the
shots change order the keyframes stay put. Items the batch itself changed, and soundtrack clips fixed to the
timeline, are left alone.

A shot taken out of the sequence takes the items lying wholly on it along: they leave the project's
lists and wait in the shot's "parked" field, timed from the shot's start, until it comes back.
"""
from __future__ import annotations

import math

from .edits import MIN_ITEM, audio_length, r3, shift_keys, split_keys
from .keyframes import value_at
from .sequence import VOICE_TRACK, audio_track, build_sequence, overlay_speed, total_duration
from .util import new_id

EPS = 1e-4
CAPTION_LEFT = 0.5  # a caption cut by a deleted shot goes when less than this is left (too short to read)
LISTS =(("captions", "caption"), ("audio", "audio"), ("overlays", "overlay"), ("adjustments", "adjust"))
ID_PREFIX = {"audio": "a", "overlays": "o", "adjustments": "adj"}  # as split_ops in edits.py names new pieces


def _pieces(old_ids: set[str], new_clips: list[dict], j: int) -> list[int]:
    """New clip j and the pieces split off it, by index: the clips added after it (up to the next shot that was
    there before) that play on from the same file at the same speed. Something can sit in between, as a freeze
    frame does."""
    head = new_clips[j]
    out = [j]
    if head["source"] != "video":
        return out
    for m in range(j + 1, len(new_clips)):
        c = new_clips[m]
        if c["shot"]["id"] in old_ids:
            break
        if c["source"] == "video" and c["take"].get("file") == head["take"].get("file") \
                and c["speed"] == head["speed"] and c["in"] >= new_clips[out[-1]]["out"] - EPS:
            out.append(m)
    return out


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
        if (old.get("take") or {}).get("id") != (new.get("take") or {}).get("id"):
            # Another take (a reversed or stabilized copy, another version) runs on its own source clock, so
            # its trims say nothing about where the old frames went: the point keeps its place in the clip.
            return new["start"] - old["start"]
        # A point trimmed off the head of its clip sticks to the clip's new start. Points follow the
        # source frame they sat on, so a speed change stretches them along with the clip.
        source = old["in"] + local * old.get("speed", 1.0)
        if source >= new["out"] - EPS:  # past the head of a split: on its tail, maybe beyond a freeze frame
            old_ids = {c["shot"]["id"] for c in old_clips}
            pieces = _pieces(old_ids, new_clips, next(m for m, c in enumerate(new_clips) if c is new))[1:]
            new = next((new_clips[m] for m in pieces if new_clips[m]["in"] - EPS <= source < new_clips[m]["out"]), new)
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


def _gone(clips: list[dict], shots: set[str]) -> list[tuple[float, float]]:
    """The stretches of the sequence the clips of `shots` held (a transition belongs to the later clip)."""
    out = []
    for i, clip in enumerate(clips):
        if clip["shot"]["id"] in shots:
            end = clips[i + 1]["start"] if i + 1 < len(clips) else clip["start"] + clip["length"]
            out.append((clip["start"], end))
    return out


def _left(start: float, end: float, gone: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    """The parts of start..end outside the `gone` stretches, as (from, to, anchor): anchor is where the part
    is pinned, the start of the stretch it closes up to. Slivers shorter than an item can be are dropped;
    an item no stretch touches comes back whole, however short."""
    if not any(a < end and b > start for a, b in gone):
        return [(start, end, start)]
    parts, at, anchor = [], start, start
    for a, b in gone:
        if b <= at or a >= end:
            continue
        if a > at:
            parts.append((at, a, anchor))
        at, anchor = max(at, b), a
    if at < end:
        parts.append((at, end, anchor))
    return [p for p in parts if p[1] - p[0] >= MIN_ITEM - EPS]  # 5.718 - 5.618 is just under 0.1


def _piece(project: dict, key: str, item: dict, offset: float, length: float, full: float) -> dict:
    """The part of an item from `offset` seconds into it, `length` long."""
    tail_cut = offset + length < full - 1e-3
    out = {**item, "start": r3(float(item.get("start") or 0.0) + offset)}
    if item.get("keyframes"):
        keys = shift_keys(item, offset)
        out["keyframes"] = split_keys({**item, "keyframes": keys}, length)[0] if tail_cut else keys
    if key == "audio":
        speed = float(item.get("speed") or 1.0)
        t_in = float(item.get("in") or 0.0)
        out["in"] = r3(t_in + offset * speed)
        if offset > 0:
            out["fadeIn"] = 0
        if tail_cut:
            out["out"], out["fadeOut"] = r3(t_in + (offset + length) * speed), 0
        return out
    still = (project.get("media") or {}).get(item.get("mediaId"), {}).get("kind") == "image"
    if key == "overlays" and not still:
        out["in"] = r3(float(item.get("in") or 0.0) + offset * overlay_speed(project, item))
    out["length"] = r3(length)
    return out


def _cut_ops(project: dict, key: str, name: str, item: dict, parts: list[tuple[float, float, float]],
             old_clips: list[dict], new_clips: list[dict]) -> list[dict]:
    """Ops that leave only `parts` of an item, each closed up to where its anchor lands; with none left it goes."""
    if not parts:
        return [{"op": f"{name}.delete", "id": item["id"]}]
    start = float(item.get("start") or 0.0)
    full = _end(project, key, item) - start
    ops = []
    for i, (a, b, anchor) in enumerate(parts):
        piece = _piece(project, key, item, a - start, b - a, full)
        piece["start"] = round(max(0.0, anchor + _shift(old_clips, new_clips, anchor)), 3)
        if i == 0:
            patch = {k: v for k, v in piece.items() if item.get(k) != v}
            ops.append({"op": f"{name}.update", "id": item["id"], "patch": patch})
        else:
            track = {"track": audio_track(item)} if key == "audio" else {}  # keep the pieces on one row
            added = {k: v for k, v in piece.items() if k != "captionId"}  # its caption stays with the first piece
            ops.append({"op": f"{name}.add", "item": {**added, "id": new_id(ID_PREFIX[key]), **track}})
    return ops


def _library_sound(key: str, item: dict) -> bool:
    """Music or a sound effect from the library, not a shot's detached sound (those carry their shot and take)
    or a clip on the voice track (a voice-over keeps in step with its caption, a recording is cut like sound)."""
    return key == "audio" and not item.get("takeId") and not item.get("shotId") and audio_track(item) != VOICE_TRACK


def _spoken_line(project: dict, item: dict) -> bool:
    """A voice-over made from its caption's text: it goes with the caption and is never cut in two. A recording
    bound to a caption (live sound on the voice track) is cut across deleted shots like detached sound."""
    source = ((project.get("media") or {}).get(item.get("mediaId")) or {}).get("source") or {}
    return bool(item.get("captionId")) and bool(source.get("captionId"))


Segment = tuple[float, float, float, float]  # old from, old to, new from, rate


def _segments(old_clips: list[dict], new_clips: list[dict]) -> list[Segment] | None:
    """Where the old sequence lands on the new one, as stretches in time order. What lies between them on the
    old sequence was cut (deleted, trimmed off), what lies between them on the new one was added (a longer
    shot, an inserted one, a freeze frame). None when the shots changed order: keyframes then stay as they are."""
    old_ids = {c["shot"]["id"] for c in old_clips}
    pos = {c["shot"]["id"]: j for j, c in enumerate(new_clips)}
    order = [pos[c["shot"]["id"]] for c in old_clips if c["shot"]["id"] in pos]
    if order != sorted(order):
        return None
    old_end, new_end = total_duration(old_clips), total_duration(new_clips)

    def stop(clips: list[dict], i: int, end: float) -> float:  # where clip i's stretch ends
        return clips[i + 1]["start"] if i + 1 < len(clips) else end

    segs: list[Segment] = []
    for i, old in enumerate(old_clips):
        j = pos.get(old["shot"]["id"])
        if j is None:
            continue
        new = new_clips[j]
        if old["source"] == "video" and (old.get("take") or {}).get("id") == (new.get("take") or {}).get("id"):
            # Follow the source frames, as _shift does, onto the clip and the pieces split off it: a trimmed-off
            # head or tail drops out, a speed change stretches.
            lo, hi = old["in"], old["in"] + (stop(old_clips, i, old_end) - old["start"]) * old["speed"]
            for m in _pieces(old_ids, new_clips, j):
                c = new_clips[m]
                w0 = max(lo, c["in"])
                w1 = min(hi, c["in"] + (stop(new_clips, m, new_end) - c["start"]) * c["speed"])
                if w1 - w0 > EPS:
                    segs.append((old["start"] + (w0 - old["in"]) / old["speed"],
                                 old["start"] + (w1 - old["in"]) / old["speed"],
                                 c["start"] + (w0 - c["in"]) / c["speed"], old["speed"] / c["speed"]))
            continue
        # A still, an empty shot or another take keeps its place in the clip; its room runs on over shots added
        # after it, so a split's tail carries on from its head.
        room = next((c["start"] for c in new_clips[j + 1:] if c["shot"]["id"] in old_ids), new_end)
        lo, at = old["start"], new["start"]
        hi = min(stop(old_clips, i, old_end), lo + room - at)
        if hi - lo > EPS:
            segs.append((lo, hi, at, 1.0))
    segs.append((old_end, math.inf, new_end, 1.0))  # what runs on past the picture
    return segs


def _follow_keys(item: dict, start: float, end: float, new_start: float, segs: list[Segment]) -> list[dict]:
    """The keyframes of an item lying on start..end, moved to follow the picture, in seconds into it now that
    it starts at new_start. Keys over cut picture drop out, and a key where it closed up holds the value from
    just past the cut; over added picture the value where it went in holds."""
    keys = item.get("keyframes") or []
    if not any(o0 < end and o1 > start for o0, o1, _, _ in segs):
        return keys  # it lay wholly on cut picture and moved as one piece
    fields = list(dict.fromkeys(f for k in keys for f in k if f != "t"))

    def rel(n: float) -> float:
        return r3(n - new_start) + 0.0  # no -0.0

    out: dict[float, dict] = {}
    for k in keys:
        t = start + float(k["t"])
        seg = next((s for s in segs if s[0] - EPS <= t < s[1]), None)
        if seg is None:
            continue
        n = seg[2] + (t - seg[0]) * seg[3]
        out[rel(n)] = k if abs(n - new_start - float(k["t"])) <= EPS else {**k, "t": rel(n)}
    a, left = 0.0, 0.0  # the end of the previous stretch, on the old and the new sequence
    for o0, o1, n0, rate in segs:
        b, right = o0, n0
        cut, added = b - a > EPS, right - left > EPS
        if (cut and a < end and b > start) or (added and start < a < end):
            edge = b if b < end else a  # the item running out on cut picture holds the value from before it
            hold = {f: value_at(item, f, edge - start) for f in fields}
            for n in (left, right):
                out.setdefault(rel(n), {**hold, "t": rel(n)})
        a, left = o1, n0 + (o1 - o0) * rate
    return sorted((k for k in out.values() if k["t"] >= 0), key=lambda k: k["t"])


def _whole_ops(project: dict, item: dict, anchor: float, old_clips: list[dict], new_clips: list[dict],
               segs: list[Segment] | None) -> list[dict]:
    """A library sound kept whole across deleted shots: it starts where its anchor lands and plays on uncut,
    while its keyframes follow the picture."""
    start = float(item.get("start") or 0.0)
    new_start = round(max(0.0, anchor + _shift(old_clips, new_clips, anchor)), 3)
    patch: dict = {"start": new_start} if abs(new_start - start) > EPS else {}
    keys = item.get("keyframes") or []
    if keys and segs:
        new_keys = _follow_keys(item, start, _end(project, "audio", item), new_start, segs)
        if new_keys != keys:
            patch["keyframes"] = new_keys
    return [{"op": "audio.update", "id": item["id"], "patch": patch}] if patch else []


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
    delete clears the linked tracks; a caption running past it loses the part over it. On the other tracks
    the stretch over it is cut out, recordings bound to a caption included, except that library music and
    sound effects and voice-overs stay whole (only the music's keyframes close up). A shot taken out
    of the sequence parks the items lying wholly on it, and puts them back when it returns. With shift
    False only that putting back happens.
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

    gone = _gone(old_clips, deleted) if deleted else []
    caption_moved: dict[str, float] = {}  # caption id -> how far its start moved
    for cap in after.get("captions") or []:
        start = float(cap.get("start") or 0.0)
        if old_starts.get(("captions", cap["id"])) != start:
            continue
        end = float(cap.get("end") or 0.0)
        # The part over a deleted shot is cut out and the rest closes up; with too little left to read it goes.
        parts = _left(start, end, gone) if gone else [(start, end, start)]
        trimmed = parts != [(start, end, start)]
        if not parts or (trimmed and sum(b - a for a, b, _ in parts) < CAPTION_LEFT - EPS):
            dropped.add(cap["id"])
            ops.append({"op": "caption.delete", "id": cap["id"]})
            continue
        clip = _owner(old_clips, taken_out, start, end) if taken_out else None
        if clip:
            parked_caps[cap["id"]] = clip["shot"]["id"]
            park(clip, "captions", "caption", cap)
            continue
        kept, _, anchor = parts[0]
        new_start = round(max(0.0, anchor + _shift(old_clips, new_clips, anchor)), 3)
        new_end = round(new_start + sum(b - a for a, b, _ in parts), 3)
        caption_shift[cap["id"]] = new_start - kept  # how far the moments still under it moved
        caption_moved[cap["id"]] = new_start - start
        if abs(new_start - start) <= EPS and abs(new_end - end) <= EPS:
            continue
        ops.append({"op": "caption.update", "id": cap["id"], "patch": {"start": new_start, "end": new_end}})

    by_shot = {c["shot"]["id"]: c for c in old_clips}
    segs = _segments(old_clips, new_clips)
    for key, name in LISTS[1:]:
        for item in after.get(key) or []:
            start = float(item.get("start") or 0.0)
            if old_starts.get((key, item["id"])) != start or item.get("fixed"):
                continue
            # A voice-over keeps in step with its caption; a recording bound to one is cut like detached sound
            # and, when its caption goes, stays without it.
            cid = item.get("captionId") if key == "audio" else None
            spoken = bool(cid) and _spoken_line(after, item)
            if spoken and cid in dropped:
                ops.append({"op": "audio.delete", "id": item["id"]})
                continue
            unbind = {"captionId": None} if cid in dropped else {}
            clip = by_shot[parked_caps[cid]] if cid in parked_caps else None
            if clip is None and taken_out and cid not in caption_shift:
                clip = _owner(old_clips, taken_out, start, _end(after, key, item))
            if clip:
                park(clip, key, name, item)
                continue
            end = _end(after, key, item)
            parts = _left(start, end, gone) if gone and not spoken else [(start, end, start)]
            if parts != [(start, end, start)]:
                if parts and _library_sound(key, item):
                    ops += _whole_ops(after, item, parts[0][2], old_clips, new_clips, segs)
                else:
                    cut = _cut_ops(after, key, name, item, parts, old_clips, new_clips)
                    if parts and unbind:
                        cut[0]["patch"].update(unbind)
                    ops += cut
                continue
            if spoken and cid in caption_moved:
                d = caption_moved[cid]
            elif cid in caption_shift:
                d = caption_shift[cid]
            else:
                d = _shift(old_clips, new_clips, start)
            new_start = round(max(0.0, start + d), 3)
            patch = {"start": new_start, **unbind} if abs(d) > EPS else {**unbind}
            if segs and item.get("keyframes") and _library_sound(key, item):
                keys = _follow_keys(item, start, end, new_start, segs)
                if keys != item["keyframes"]:
                    patch["keyframes"] = keys
            if patch:
                ops.append({"op": f"{name}.update", "id": item["id"], "patch": patch})
    for sid, lists in parked.items():
        ops.append({"op": "shot.update", "id": sid, "patch": {"parked": lists}})
    return ops
