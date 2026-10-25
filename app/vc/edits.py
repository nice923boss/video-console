"""Timeline edits by name, for scripts and Claude (GET /timeline, POST /edit in api/projects.py).

Each command reads the project and returns the ops the edit page's keys would send
(web/js/components/tl-edit.js and tl-commands.js), so one call is one undo step. Times are timeline
seconds unless a name says otherwise; "type" is shot, overlay, audio, caption or adjust.
"""
from __future__ import annotations

from typing import Any

from .grade import clean_grade
from .keyframes import value_at
from .sequence import VOICE_TRACK, audio_media, audio_track, build_sequence, total_duration, video_take
from .util import AppError, new_id

LISTS = {"overlay": "overlays", "audio": "audio", "caption": "captions", "adjust": "adjustments"}
MIN_VIDEO, MIN_STILL, MIN_ITEM = 0.1, 0.2, 0.1
VOLUME_MAX = 4.0
RAMP = 0.1  # seconds a range's volume takes to rise or fall to its level
FREEZE_LEN = 2.0
EDGE = 0.1
SPLIT_LABEL = {"shot": "分割鏡頭", "overlay": "分割片段", "audio": "分割音軌", "adjust": "分割調整圖層", "caption": "分割字幕"}


def r3(v: float) -> float:
    return round(float(v), 3)


# ---------------------------------------------------------------- keyframes (mirror of lib/keyframes.js)
def with_key(item: dict, t: float, values: dict) -> list[dict]:
    """Keyframes with one more key at t, merged into a key already there."""
    at = r3(t)
    keys = item.get("keyframes") or []
    old = next((k for k in keys if abs(k["t"] - at) <= 1e-3), {})
    rest = [k for k in keys if abs(k["t"] - at) > 1e-3]
    return sorted([*rest, {**old, **values, "t": at}], key=lambda k: k["t"])


def split_keys(item: dict, d: float) -> tuple[list[dict], list[dict]]:
    """Keyframes of the two halves of a clip cut d seconds in; both get a key holding the value at the cut."""
    keys = item.get("keyframes") or []
    fields = list(dict.fromkeys(f for k in keys for f in k if f != "t"))
    edge = {f: value_at(item, f, d) for f in fields}
    head = [*(k for k in keys if k["t"] < d - 1e-3), {**edge, "t": r3(d)}]
    tail = [{**edge, "t": 0.0}, *({**k, "t": r3(k["t"] - d)} for k in keys if k["t"] > d + 1e-3)]
    return head, tail


def shift_keys(item: dict, d: float) -> list[dict]:
    """Keyframes once the clip's start moves d seconds later while its content stays put."""
    keys = item.get("keyframes") or []
    if not keys or not d:
        return keys
    if d < 0:
        return [{**k, "t": r3(k["t"] - d)} for k in keys]
    return split_keys(item, d)[1]


def key_patch(item: dict, keys: list[dict]) -> dict:
    return {"keyframes": keys} if item.get("keyframes") else {}


# ---------------------------------------------------------------- reading the timeline
def audio_length(project: dict, item: dict) -> float:
    media = audio_media(project, item) or {}
    full = float(media.get("duration") or 0.0)
    t_out = full if item.get("out") is None else float(item["out"])
    return max(0.1, (t_out - float(item.get("in") or 0.0)) / float(item.get("speed") or 1.0))


def track_busy(project: dict, key: str, start: float, length: float) -> bool:
    """Whether a clip on music track `key` plays somewhere in [start, start + length), so the track can't take
    another clip there (mirror of trackBusy in components/tl-tracks.js). The voice rows never are."""
    if key == VOICE_TRACK:
        return False
    return any(
        audio_track(a) == key
        and start < float(a.get("start") or 0.0) + audio_length(project, a) - 1e-3
        and float(a.get("start") or 0.0) < start + length - 1e-3
        for a in project.get("audio", [])
    )


def _span(project: dict, kind: str, item: dict) -> tuple[float, float]:
    start = float(item.get("start") or 0.0)
    if kind == "audio":
        return start, audio_length(project, item)
    if kind == "caption":
        return start, float(item.get("end") or 0.0) - start
    return start, float(item.get("length") or MIN_ITEM)


def _is_still(project: dict, item: dict) -> bool:
    return (project.get("media") or {}).get(item.get("mediaId"), {}).get("kind") == "image"


def _clips(project: dict) -> list[dict]:
    pos = {s["id"]: i for i, s in enumerate(project["shots"])}
    clips = build_sequence(project)
    for clip in clips:
        clip["index"] = pos[clip["shot"]["id"]]
    return clips


def _clip_at(clips: list[dict], t: float) -> dict | None:
    """The clip that owns time t (the later clip inside a transition overlap)."""
    hit = None
    for clip in clips:
        if clip["start"] <= t + 1e-6:
            hit = clip
        else:
            break
    return hit


def timeline(project: dict) -> dict:
    """Where everything sits: picture clips in order, the items on the other tracks, markers."""
    clips = _clips(project)
    out_clips = [{
        "shotId": c["shot"]["id"], "index": c["index"], "name": c["shot"].get("name") or "", "source": c["source"],
        "takeId": (c["take"] or {}).get("id"), "start": r3(c["start"]), "length": r3(c["length"]),
        "in": r3(c["in"]), "out": r3(c["out"]), "speed": c["speed"], "disabled": bool(c["shot"].get("disabled")),
        "audioDetached": bool(c["shot"].get("audioDetached")),
    } for c in clips]
    items = []
    for kind, key in LISTS.items():
        for item in project.get(key) or []:
            start, length = _span(project, kind, item)
            items.append({"type": kind, "id": item["id"], "start": r3(start), "length": r3(length),
                          "name": item.get("name") or (item.get("text") or "")[:40],
                          "keyframes": len(item.get("keyframes") or [])})
    items.sort(key=lambda x: (x["start"], x["type"]))
    markers = sorted(project.get("markers") or [], key=lambda m: m["start"])
    return {"duration": total_duration(clips), "clips": out_clips, "items": items, "markers": markers}


# ---------------------------------------------------------------- targets
def _target(project: dict, clips: list[dict], kind: str, item_id: str | None, t: float) -> dict:
    """The clip an edit at t acts on: {type, item, start, length, min, clip?, still?}."""
    if kind == "shot":
        clip = next((c for c in clips if c["shot"]["id"] == item_id), None) if item_id else _clip_at(clips, t)
        if not clip:
            raise AppError("找不到這個鏡頭，或它不在剪輯序列裡" if item_id else "這個時間點沒有鏡頭")
        return {"type": "shot", "item": clip["shot"], "clip": clip, "start": clip["start"], "length": clip["length"],
                "min": MIN_VIDEO if clip["source"] == "video" else MIN_STILL}
    if kind not in LISTS:
        raise AppError("type 只能是 shot、overlay、audio、caption 或 adjust")
    items = project.get(LISTS[kind]) or []
    if item_id:
        item = next((x for x in items if x["id"] == item_id), None)
        if not item:
            raise AppError("找不到這個片段")
    else:
        item = next((x for x in items if _covers(project, kind, x, t)), None)
        if not item:
            raise AppError("這個時間點在這一軌沒有片段")
    start, length = _span(project, kind, item)
    still = kind == "overlay" and _is_still(project, item)
    return {"type": kind, "item": item, "clip": None, "start": start, "length": length, "still": still,
            "min": MIN_STILL if still else MIN_ITEM}


def _covers(project: dict, kind: str, item: dict, t: float) -> bool:
    start, length = _span(project, kind, item)
    return start < t < start + length


def _local(g: dict, t: float) -> float:
    local = t - g["start"]
    if local < g["min"] or g["length"] - local < g["min"]:
        raise AppError("時間點太靠近片段邊緣（或不在片段上），無法在這裡剪")
    return local


# ---------------------------------------------------------------- split and trim
def _tail_versions(project: dict, shot: dict, tail_id: str) -> tuple[list[dict], dict]:
    """Copies of the shot's versions for the tail of a cut (same files, ids of its own), so each half switches
    and regenerates on its own; a copy keeps its pairing (a reversed copy's parent) and the active picks follow."""
    takes = project.get("takes") or {}
    own = {k: x for k, x in takes.items() if x.get("shot") == shot["id"]}
    for key in ("activeVideo", "activeImage"):
        if shot.get(key) in takes:
            own.setdefault(shot[key], takes[shot[key]])  # a half cut before versions were copied
    ids = {k: new_id("t") for k in own}
    ops = [{"op": "take.add", "take": {**x, "id": ids[k], "shot": tail_id,
                                        **({"parent": ids[x["parent"]]} if x.get("parent") in ids else {})}}
           for k, x in own.items()]
    return ops, {key: ids[shot[key]] for key in ("activeVideo", "activeImage") if shot.get(key) in ids}


def split_ops(g: dict, t: float, local: float, project: dict) -> list[dict]:
    """The ops that cut a target in two, local seconds into it (t on the timeline)."""
    item, kind = g["item"], g["type"]
    if kind == "shot":
        clip = g["clip"]
        trans = item.get("transition") or {"type": "cut", "duration": 0.5}
        at = r3(clip["in"] + local * clip["speed"])
        video = clip["source"] == "video"
        head = {"trimOut": at} if video else {"duration": round(local, 2)}
        tail = {"trimIn": at, "trimOut": item.get("trimOut")} if video else {"duration": round(clip["length"] - local, 2)}
        sid = new_id("s")
        copies, active = _tail_versions(project, item, sid)
        return [{"op": "shot.update", "id": item["id"], "patch": {**head, "transition": {**trans, "type": "cut"}}},
                *copies, {"op": "shot.add", "shot": {**item, **tail, **active, "id": sid}, "index": clip["index"] + 1}]
    head_keys, tail_keys = split_keys(item, local)
    t = r3(t)
    if kind == "overlay":
        moved = {} if g["still"] else {"in": r3(float(item.get("in") or 0) + local)}
        rest = {**item, "id": new_id("o"), "start": t, **moved, "length": r3(g["length"] - local),
                **key_patch(item, tail_keys)}
        return [{"op": "overlay.update", "id": item["id"], "patch": {"length": r3(local), **key_patch(item, head_keys)}},
                {"op": "overlay.add", "item": rest}]
    if kind == "audio":
        cut = r3(float(item.get("in") or 0) + local * float(item.get("speed") or 1))
        return [{"op": "audio.update", "id": item["id"], "patch": {"out": cut, "fadeOut": 0, **key_patch(item, head_keys)}},
                {"op": "audio.add", "item": {**item, "id": new_id("a"), "track": audio_track(item), "start": t, "in": cut,
                                             "fadeIn": 0, **key_patch(item, tail_keys)}}]
    if kind == "adjust":
        return [{"op": "adjust.update", "id": item["id"], "patch": {"length": r3(local), **key_patch(item, head_keys)}},
                {"op": "adjust.add", "item": {**item, "id": new_id("adj"), "start": t, "length": r3(g["length"] - local),
                                              **key_patch(item, tail_keys)}}]
    return [{"op": "caption.update", "id": item["id"], "patch": {"end": t}},
            {"op": "caption.add", "item": {**item, "id": new_id("c"), "start": t, "shot": None}}]


def split(project: dict, t: float, kind: str = "shot", item_id: str | None = None) -> dict:
    g = _target(project, _clips(project), kind, item_id, t)
    ops = split_ops(g, t, _local(g, t), project)
    tail = ops[-1].get("shot") or ops[-1]["item"]
    return {"ops": ops, "label": SPLIT_LABEL[kind], "id": tail["id"]}


def split_all(project: dict, t: float) -> dict:
    """Cut every clip on every track that t crosses; clips too close to an edge stay whole."""
    clips = _clips(project)
    targets = []
    clip = _clip_at(clips, t)
    if clip:
        targets.append(_target(project, clips, "shot", clip["shot"]["id"], t))
    for kind, key in LISTS.items():
        targets += [_target(project, clips, kind, x["id"], t) for x in project.get(key) or []]
    ops: list[dict] = []
    for g in targets:
        local = t - g["start"]
        if local >= g["min"] and g["length"] - local >= g["min"]:
            ops += split_ops(g, t, local, project)
    if not ops:
        raise AppError("這個時間點沒有可以分割的片段")
    return {"ops": ops, "label": "分割所有軌道"}


def trim(project: dict, t: float, side: str, kind: str = "shot", item_id: str | None = None) -> dict:
    """side "start" removes the part before t, "end" the part after it. Later shots close up on the picture lane."""
    if side not in ("start", "end"):
        raise AppError("side 只能是 start 或 end")
    g = _target(project, _clips(project), kind, item_id, t)
    local = _local(g, t)
    item, left, t = g["item"], side == "start", r3(t)
    label = "向左裁剪" if left else "向右裁剪"
    if kind == "shot":
        clip = g["clip"]
        at = r3(clip["in"] + local * clip["speed"])
        if clip["source"] == "video":
            patch = {"trimIn": at} if left else {"trimOut": at}
        else:
            patch = {"duration": round(clip["length"] - local if left else local, 2)}
    elif kind == "overlay":
        t_in = item.get("in") if g["still"] else r3(float(item.get("in") or 0) + local)
        patch = ({"start": t, "in": t_in, "length": r3(g["length"] - local), **key_patch(item, shift_keys(item, local))}
                 if left else {"length": r3(local)})
    elif kind == "audio":
        cut = r3(float(item.get("in") or 0) + local * float(item.get("speed") or 1))
        patch = {"start": t, "in": cut, **key_patch(item, shift_keys(item, local))} if left else {"out": cut}
    elif kind == "adjust":
        patch = ({"start": t, "length": r3(g["length"] - local), **key_patch(item, shift_keys(item, local))}
                 if left else {"length": r3(local)})
    else:
        patch = {"start": t} if left else {"end": t}
    return {"ops": [{"op": f"{kind}.update", "id": item["id"], "patch": patch}], "label": label}


# ---------------------------------------------------------------- picture and sound
def freeze(project: dict, t: float) -> dict:
    """Cut the video clip at t and put a FREEZE_LEN still of that frame in between (before or after at an edge)."""
    clip = _clip_at(_clips(project), t)
    if not clip or clip["source"] != "video":
        raise AppError("這個時間點要是影片鏡頭才能定格")
    local = t - clip["start"]
    at = r3(min(clip["out"] - 0.04, clip["in"] + local * clip["speed"]))
    sid = new_id("s")
    still = {"op": "shot.add", "shot": {"id": sid, "name": "定格", "duration": FREEZE_LEN}, "index": clip["index"] + 1}
    if local < EDGE:
        ops, place = [{**still, "index": clip["index"]}], clip["start"]
    elif clip["length"] - local < EDGE:
        ops, place = [still], clip["start"] + clip["length"]
    else:
        g = {"type": "shot", "item": clip["shot"], "clip": clip}
        ops, place = [*split_ops(g, t, local, project), still], t
    ops += _detached_sound_ops(project, clip, place)
    job = {"kind": "lastframe", "shotId": clip["shot"]["id"],
           "payload": {"source": clip["take"]["id"], "target": "shot", "targetShot": sid, "at": at}}
    return {"ops": ops, "label": "定格", "job": job, "id": sid}


def _detached_sound_ops(project: dict, clip: dict, place: float) -> list[dict]:
    """Keep the shot's detached sound under its picture when a still goes in at `place`, whatever the link
    toggle: a clip across `place` is cut there and its tail moves past the still, later ones move by its length."""
    ops: list[dict] = []
    for item in _footage_sound(project, clip["shot"]["id"], _file_of(project, clip["take"]["id"])):
        start = float(item.get("start") or 0.0)
        local = place - start
        if local >= MIN_ITEM and audio_length(project, item) - local >= MIN_ITEM:
            head, tail = split_ops({"type": "audio", "item": item}, place, local, project)
            ops += [head, {**tail, "item": {**tail["item"], "start": r3(place + FREEZE_LEN)}}]
        elif local < MIN_ITEM:
            ops.append({"op": "audio.update", "id": item["id"], "patch": {"start": r3(start + FREEZE_LEN)}})
    return ops


def _file_of(project: dict, take_id: str | None) -> str | None:
    """The footage a version shows; a reversed copy counts as the footage it was made from."""
    takes = project.get("takes") or {}
    take = takes.get(take_id) or {}
    if _is_reverse(take):
        take = takes.get(take.get("parent")) or take
    return take.get("file")


def _footage_sound(project: dict, shot_id: str, file: str | None) -> list[dict]:
    """The detached sound of a shot and of the shots cut from the same footage."""
    return [a for a in project.get("audio") or []
            if a.get("shotId") and (a["shotId"] == shot_id or (file and _file_of(project, a.get("takeId")) == file))]


def detach(project: dict, shot_id: str | None = None, t: float | None = None) -> dict:
    """The shot's sound becomes an audio clip under it (takeId + shotId) and the shot plays silent."""
    clips = _clips(project)
    clip = (next((c for c in clips if c["shot"]["id"] == shot_id), None) if shot_id
            else _clip_at(clips, t) if t is not None else None)
    if not clip:
        raise AppError("找不到這個鏡頭，或它不在剪輯序列裡")
    shot = clip["shot"]
    if shot.get("audioDetached"):
        raise AppError("這個鏡頭的原聲已經分離了")
    if clip["source"] != "video" or not clip["take"].get("hasAudio"):
        raise AppError("這個鏡頭沒有聲音")
    aid = new_id("a")
    # A sped shot's sound keeps its speed (the shot's speed is locked while its sound is detached).
    item = {"id": aid, "takeId": clip["take"]["id"], "shotId": shot["id"], "start": r3(clip["start"]),
            "in": r3(clip["in"]), "out": r3(clip["out"]), "name": f"{shot.get('name') or '鏡頭'} 原聲",
            **({"speed": clip["speed"]} if clip["speed"] != 1 else {})}
    return {"ops": [{"op": "audio.add", "item": item}, {"op": "shot.update", "id": shot["id"], "patch": {"audioDetached": True}}],
            "label": "分離原聲", "id": aid}


def restore(project: dict, item_id: str) -> dict:
    """Undo a detach from the shot or its audio clip; shots cut from the same footage restore together (each half
    has its own copy of the version, so they are matched by file)."""
    audio = project.get("audio") or []
    hit = next((a for a in audio if a["id"] == item_id and a.get("shotId")), None)
    shot = next((s for s in project["shots"] if s["id"] == item_id and s.get("audioDetached")), None)
    shot_id = hit["shotId"] if hit else shot["id"] if shot else None
    if not shot_id:
        raise AppError("這不是已分離原聲的鏡頭或它的原聲片段")
    files = {_file_of(project, a.get("takeId")) for a in audio if a.get("shotId") == shot_id} - {None}
    items = [a for a in audio if a.get("shotId") and (a["shotId"] == shot_id or _file_of(project, a.get("takeId")) in files)]
    shots = [s for s in project["shots"]
             if s.get("audioDetached") and (s["id"] == shot_id or _file_of(project, s.get("activeVideo")) in files)]
    return {"ops": [*({"op": "audio.delete", "id": a["id"]} for a in items),
                    *({"op": "shot.update", "id": s["id"], "patch": {"audioDetached": False}} for s in shots)],
            "label": "還原原聲"}


def _is_reverse(take: dict | None) -> bool:
    return bool(take and (take.get("source") or {}).get("tool") == "reverse")


def reverse(project: dict, shot_id: str | None = None, t: float | None = None) -> dict:
    """Switch to the reversed copy (or back); the first time, a "reverse" job renders it."""
    shot = (next((s for s in project["shots"] if s["id"] == shot_id), None) if shot_id
            else (_clip_at(_clips(project), t) or {}).get("shot") if t is not None else None)
    take = shot and video_take(project, shot)
    if not take:
        raise AppError("倒放只能用在影片鏡頭")
    takes = project.get("takes") or {}
    other = (takes.get(take.get("parent")) if _is_reverse(take) else
             next((x for x in takes.values() if x.get("parent") == take["id"] and _is_reverse(x)
                   and x.get("kind") == "video" and x.get("duration")), None))
    if other:
        dur = float(take["duration"])
        trims = {"trimIn": r3(max(0.0, dur - shot["trimOut"])) if shot.get("trimOut") is not None else 0,
                 "trimOut": r3(dur - shot["trimIn"]) if (shot.get("trimIn") or 0) > 0 else None}
        return {"ops": [{"op": "shot.update", "id": shot["id"], "patch": {"activeVideo": other["id"], **trims}}],
                "label": "取消倒放" if _is_reverse(take) else "倒放"}
    if _is_reverse(take):
        raise AppError("找不到倒放前的原始影片")
    return {"ops": [], "label": "倒放", "job": {"kind": "reverse", "shotId": shot["id"], "payload": {"source": take["id"]}}}


def volume(project: dict, kind: str, item_id: str, db: float, t_in: float | None = None,
           t_out: float | None = None) -> dict:
    """Change an audio or upper-track clip's volume by db; with t_in..t_out, only inside that range."""
    if kind == "shot":
        raise AppError("鏡頭的原聲要先分離成音訊片段（cmd: detach），才能調整音量")
    if kind not in ("audio", "overlay"):
        raise AppError("只有 audio 與 overlay 片段有音量")
    g = _target(project, [], kind, item_id, 0.0)
    item, length = g["item"], g["length"]
    if g["still"]:
        raise AppError("圖片沒有聲音")
    f = 10 ** (db / 20)

    def scale(v: float) -> float:
        return round(min(VOLUME_MAX, max(float(v), 0.001) * f), 4)

    def base(t: float) -> float:
        return float(value_at(item, "volume", t, 1.0))

    ranged = t_in is not None and t_out is not None
    if not ranged and (t_in is not None or t_out is not None):
        raise AppError("in 與 out 要一起給，或都不給（整段）")
    a = min(max(t_in - g["start"], 0.0), length) if ranged else 0.0
    b = min(max(t_out - g["start"], 0.0), length) if ranged else length
    if b - a < 0.01:
        raise AppError("區域沒有蓋到這個片段")
    keys = item.get("keyframes") or []
    if a > 1e-3 or b < length - 1e-3:
        work = {"keyframes": [{**k, "volume": scale(k["volume"])} if "volume" in k and a - 1e-3 <= k["t"] <= b + 1e-3 else k
                              for k in keys]}
        if a > 1e-3:
            work["keyframes"] = with_key(work, max(0.0, a - RAMP), {"volume": base(max(0.0, a - RAMP))})
        if b < length - 1e-3:
            work["keyframes"] = with_key(work, min(length, b + RAMP), {"volume": base(min(length, b + RAMP))})
        work["keyframes"] = with_key(work, a, {"volume": scale(base(a))})
        patch = {"keyframes": with_key(work, b, {"volume": scale(base(b))})}
    elif any("volume" in k for k in keys):
        patch = {"keyframes": [{**k, "volume": scale(k["volume"])} if "volume" in k else k for k in keys]}
    else:
        patch = {"volume": scale(item.get("volume", 1.0))}
    return {"ops": [{"op": f"{kind}.update", "id": item["id"], "patch": patch}],
            "label": "調高音量" if db > 0 else "調低音量"}


KEY_FIELDS = {"audio": {"volume": 1.0}, "overlay": {"x": 0.0, "y": 0.0, "w": 0.35, "opacity": 1.0, "volume": 1.0},
              "adjust": {"grade": None}}


def keyframe(project: dict, kind: str, item_id: str, t: float, values: dict | None = None) -> dict:
    """A key at t holding what the clip shows and plays there; values (field: value) replace those."""
    if kind not in KEY_FIELDS:
        raise AppError("只有 overlay、audio 與 adjust 片段能加關鍵幀；鏡頭與字幕不能加")
    g = _target(project, [], kind, item_id, t)
    item = g["item"]
    if not g["start"] - 1e-3 <= t <= g["start"] + g["length"] + 1e-3:
        raise AppError("時間點不在這個片段上")
    local = min(max(t - g["start"], 0.0), g["length"])
    fields = dict(KEY_FIELDS[kind])
    if g["still"]:
        del fields["volume"]
    extra = set(values or {}) - set(fields)
    if extra:
        raise AppError(f"這種片段的關鍵幀只能記：{'、'.join(fields)}")
    if kind == "adjust":
        current = {"grade": clean_grade(value_at(item, "grade", local))}
    else:
        current = {f: round(float(value_at(item, f, local, d)), 4) for f, d in fields.items()}
    return {"ops": [{"op": f"{kind}.update", "id": item["id"],
                     "patch": {"keyframes": with_key(item, local, {**current, **(values or {})})}}],
            "label": "加入關鍵幀"}


# ---------------------------------------------------------------- dispatch
def _num(data: dict, key: str, required: bool = True) -> float | None:
    v = data.get(key)
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise AppError(f"{key} 要是數字")
    return float(v)


def _id(data: dict, key: str = "id") -> str | None:
    v = data.get(key)
    return str(v) if v else None


def plan(project: dict, data: dict) -> dict:
    """{ops, label, job?, id?} for one command: data is {"cmd": ..., parameters}."""
    cmd = data.get("cmd")
    kind = str(data.get("type") or "shot")
    if cmd == "split":
        return split(project, _num(data, "t"), kind, _id(data))
    if cmd == "splitAll":
        return split_all(project, _num(data, "t"))
    if cmd == "trim":
        return trim(project, _num(data, "t"), str(data.get("side") or ""), kind, _id(data))
    if cmd == "freeze":
        return freeze(project, _num(data, "t"))
    if cmd == "detach":
        return detach(project, _id(data), _num(data, "t", False))
    if cmd == "restore":
        if not _id(data):
            raise AppError("restore 要給 id（鏡頭或它的原聲片段）")
        return restore(project, _id(data))
    if cmd == "reverse":
        return reverse(project, _id(data), _num(data, "t", False))
    if cmd == "volume":
        if not _id(data):
            raise AppError("volume 要給片段 id")
        return volume(project, kind, _id(data), _num(data, "db"), _num(data, "in", False), _num(data, "out", False))
    if cmd == "keyframe":
        values = data.get("values")
        if values is not None and not isinstance(values, dict):
            raise AppError("values 要是 {欄位: 值}")
        if not _id(data):
            raise AppError("keyframe 要給片段 id")
        return keyframe(project, kind, _id(data), _num(data, "t"), values)
    raise AppError("不支援的剪輯指令，可用：split、splitAll、trim、freeze、detach、restore、reverse、volume、keyframe")
