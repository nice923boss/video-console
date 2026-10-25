"""Project edit operations.

Every operation is idempotent (re-applying it is harmless) and returns its inverse so the
store can offer undo/redo. Operations mutate the working copy passed in; the store commits
the copy only when the whole batch succeeds. What each field accepts is in opfields.py.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from .linked import follow_ops
from .opfields import (
    ADJUST_FIELDS, AUDIO_FIELDS, CAPTION_FIELDS, CAPTION_STYLE_FIELDS, CAPTION_TRACK_FIELDS, CAST_FIELDS, LAYER_FIELDS, MARKER_FIELDS,
    MAX_CAPTION_STYLES, MAX_OPS, OVERLAY_FIELDS, SETTINGS_FIELDS, SHOT_FIELDS, STYLE_FIELDS, TAKE_FIELDS,
    TRACK_FIELDS, _bool, _clean_patch, _find, _float, _int, _review, _str, _tags, _update_item, clean_caption_style,
    clean_shot, snap8, valid_id,
)
from .util import AppError


# ---------------------------------------------------------------- handlers
# Each handler: (project, op) -> inverse op (or None when the op was a no-op).


def _project_update(p: dict, op: dict) -> dict | None:
    patch = op.get("patch") or {}
    inverse: dict[str, Any] = {}
    if "name" in patch:
        name = _str(120)(patch["name"]).strip() or "未命名專案"
        inverse["name"] = p["name"]
        p["name"] = name
    if "notes" in patch:
        inverse["notes"] = p.get("notes", "")
        p["notes"] = _str(8000)(patch["notes"])
    if "review" in patch:  # the client's comments on the whole film (review.py)
        inverse["review"] = p.get("review", [])
        p["review"] = _review(patch["review"])
    for key, fields in (("settings", SETTINGS_FIELDS), ("style", STYLE_FIELDS), ("tracks", TRACK_FIELDS)):
        if key in patch:
            p.setdefault(key, {})
            clean = _clean_patch(patch[key], fields)
            if key == "settings":
                for dim in ("width", "height"):
                    if dim in clean:
                        clean[dim] = snap8(clean[dim])
            inverse[key] = {k: p[key].get(k) for k in clean}
            p[key] = {**p[key], **clean}
    return {"op": "project.update", "patch": inverse} if inverse else None


def _project_replace(p: dict, op: dict) -> dict | None:
    data = op.get("project")
    if not isinstance(data, dict) or data.get("id") != p["id"]:
        raise AppError("還原資料與專案不符")
    old = copy.deepcopy({k: v for k, v in p.items() if k not in ("rev", "updated")})
    for key in list(p):
        if key not in ("id", "rev", "updated", "created"):
            p.pop(key)
    for key, value in data.items():
        if key not in ("id", "rev", "updated", "created"):
            p[key] = copy.deepcopy(value)
    return {"op": "project.replace", "project": old}


def _shot_add(p: dict, op: dict, defaults: dict) -> dict | None:
    shot = clean_shot(op.get("shot"), defaults)
    if _find(p["shots"], shot["id"]) >= 0:
        return None
    index = op.get("index")
    index = len(p["shots"]) if index is None else max(0, min(len(p["shots"]), int(index)))
    p["shots"].insert(index, shot)
    return {"op": "shot.delete", "id": shot["id"]}


def _shot_delete(p: dict, op: dict) -> dict | None:
    """Captions bound to the shot stay where they are, unbound; undo restores the binding."""
    i = _find(p["shots"], op.get("id"))
    if i < 0:
        return None
    shot = p["shots"].pop(i)
    restores = []
    for cap in p.get("captions") or []:
        if cap.get("shot") == shot["id"]:
            cap["shot"] = None
            restores.append({"op": "caption.update", "id": cap["id"], "patch": {"shot": shot["id"]}})
    add = {"op": "shot.add", "shot": shot, "index": i}
    return {"op": "batch", "ops": [add, *restores]} if restores else add


def _shot_update(p: dict, op: dict) -> dict | None:
    i = _find(p["shots"], op.get("id"))
    if i < 0:
        return None
    patch = _clean_patch(op.get("patch"), SHOT_FIELDS)
    shot = p["shots"][i]
    for key in ("activeImage", "activeVideo"):
        tid = patch.get(key)
        if tid and tid not in p["takes"]:
            raise AppError("指定的 Take 不存在")
    undo = _update_item(shot, op, patch, SHOT_FIELDS)
    return {"op": "shot.update", "id": shot["id"], **undo}


def _shot_move(p: dict, op: dict) -> dict | None:
    i = _find(p["shots"], op.get("id"))
    if i < 0:
        return None
    target = max(0, min(len(p["shots"]) - 1, int(op.get("index", i))))
    if target == i:
        return None
    shot = p["shots"].pop(i)
    p["shots"].insert(target, shot)
    return {"op": "shot.move", "id": shot["id"], "index": i}


_MEDIA_PATH = re.compile(r"^media/[A-Za-z0-9_./-]+$")


def _check_files(item: dict) -> None:
    """Files referenced by takes/media must stay inside the project's media folder."""
    for key in ("file", "thumb", "strip", "wave"):
        value = item.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not _MEDIA_PATH.match(value) or ".." in value:
            raise AppError("檔案路徑不合法")


def _media_file(v: Any) -> str:
    if not isinstance(v, str) or not _MEDIA_PATH.match(v) or ".." in v:
        raise AppError("檔案路徑不合法")
    return v


def _alpha_codec(v: Any) -> str:
    if v not in ("vp8", "vp9"):  # media.ALPHA_DECODERS
        raise AppError("不支援的透明影片編碼")
    return v


def _char_times(v: Any) -> list:
    """Each character's start in a voice file (chartimes.py); None for punctuation and spaces."""
    if not isinstance(v, list) or len(v) > 1000:
        raise AppError("逐字時間格式不正確")
    out = []
    for t in v:
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= 36000):
            raise AppError("逐字時間格式不正確")
        out.append(None if t is None else float(t))
    return out


DERIVED_FIELDS = {"wave": _media_file, "thumb": _media_file, "strip": _media_file, "alphaCodec": _alpha_codec,
                  "preview2": _media_file, "preview4": _media_file, "charTimes": _char_times}  # lowres.py, CP-13


def _item_derive(p: dict, op: dict) -> dict | None:
    """Facts found by reading a take's or media item's file again (backfill.py), not an edit by the user."""
    item = p["takes"].get(op.get("id")) or p["media"].get(op.get("id"))
    if item is None:
        return None
    patch = _clean_patch(op.get("patch"), DERIVED_FIELDS)
    undo = _update_item(item, op, patch, DERIVED_FIELDS)
    return {"op": "item.derive", "id": item["id"], **undo}


RELINK_FIELDS = {**DERIVED_FIELDS, "file": _media_file, "width": _int(0, 100000), "height": _int(0, 100000),
                 "duration": _float(0.0, 360000.0), "fps": _float(0.0, 1000.0), "hasAudio": _bool}


def _item_relink(p: dict, op: dict) -> dict | None:
    """A media item (or a take made from it) pointed at another file (LB-09); its uses stay where they are."""
    item = p["takes"].get(op.get("id")) or p["media"].get(op.get("id"))
    if item is None:
        return None
    patch = _clean_patch(op.get("patch"), RELINK_FIELDS)
    undo = _update_item(item, op, patch, RELINK_FIELDS)
    # Empty facts (a None strip from an older import) come back as absent: the validators take values only.
    empty = [k for k, v in undo["patch"].items() if v is None]
    return {"op": "item.relink", "id": item["id"], "patch": {k: v for k, v in undo["patch"].items() if v is not None},
            "unset": [*undo.get("unset", []), *empty]}


def _take_add(p: dict, op: dict) -> dict | None:
    take = op.get("take")
    if not isinstance(take, dict) or not valid_id(take.get("id")):
        raise AppError("Take 資料格式不正確")
    _check_files(take)
    if take["id"] in p["takes"]:
        return None
    p["takes"][take["id"]] = copy.deepcopy(take)
    return {"op": "take.delete", "id": take["id"]}


def _take_delete(p: dict, op: dict) -> dict | None:
    take = p["takes"].pop(op.get("id"), None)
    if take is None:
        return None
    restores = []
    for shot in p["shots"]:
        for key in ("activeImage", "activeVideo", "ref"):
            if shot.get(key) == take["id"]:
                shot[key] = None
                restores.append({"op": "shot.update", "id": shot["id"], "patch": {key: take["id"]}})
        _drop_extra_ref(shot, take["id"], restores)
    _drop_look_ref(p, take["id"], restores)
    if restores:
        return {"op": "batch", "ops": [{"op": "take.add", "take": take}, *restores]}
    return {"op": "take.add", "take": take}


def _drop_extra_ref(shot: dict, item_id: str, restores: list[dict]) -> None:
    extras = shot.get("extraRefs") or []
    if item_id in extras:
        shot["extraRefs"] = [x for x in extras if x != item_id]
        restores.append({"op": "shot.update", "id": shot["id"], "patch": {"extraRefs": extras}})


def _drop_look_ref(p: dict, item_id: str, restores: list[dict]) -> None:
    for member in p.get("cast") or []:
        if member.get("lookRef") == item_id:
            member["lookRef"] = None
            restores.append({"op": "cast.update", "id": member["id"], "patch": {"lookRef": item_id}})


def _take_update(p: dict, op: dict) -> dict | None:
    take = p["takes"].get(op.get("id"))
    if take is None:
        return None
    patch = _clean_patch(op.get("patch"), TAKE_FIELDS)
    undo = _update_item(take, op, patch, TAKE_FIELDS)
    return {"op": "take.update", "id": take["id"], **undo}


def _list_add(key: str, fields: dict) -> Callable[[dict, dict], dict | None]:
    def handler(p: dict, op: dict) -> dict | None:
        item = op.get("item")
        if not isinstance(item, dict) or not valid_id(item.get("id")):
            raise AppError("項目資料格式不正確")
        if _find(p[key], item["id"]) >= 0:
            return None
        clean = {"id": item["id"], **_clean_patch({k: v for k, v in item.items() if k in fields}, fields)}
        for extra in ("mediaId", "file"):
            if extra in item:
                clean[extra] = item[extra]
        if key == "audio" and clean.get("mediaId") not in p["media"] and clean.get("takeId") not in p["takes"]:
            raise AppError("音訊素材不存在")
        if key == "overlays":
            media = p["media"].get(clean.get("mediaId"))
            if not media or media.get("kind") not in ("image", "video"):
                raise AppError("上層畫面只能使用素材庫裡的圖片或影片")
            if _find(p["layers"], clean.get("layer")) < 0:
                raise AppError("畫面軌道不存在")
        if key == "captions" and clean.get("track") and _find(p.get("captionTracks") or [], clean["track"]) < 0:
            raise AppError("字幕軌道不存在")
        p[key].append(clean)
        p[key].sort(key=lambda it: it.get("start", 0))
        return {"op": f"{_SINGULAR[key]}.delete", "id": item["id"]}

    return handler


def _list_update(key: str, fields: dict) -> Callable[[dict, dict], dict | None]:
    def handler(p: dict, op: dict) -> dict | None:
        i = _find(p[key], op.get("id"))
        if i < 0:
            return None
        patch = _clean_patch(op.get("patch"), fields)
        if key == "overlays" and "layer" in patch and _find(p["layers"], patch["layer"]) < 0:
            raise AppError("畫面軌道不存在")
        if key == "captions" and patch.get("track") and _find(p.get("captionTracks") or [], patch["track"]) < 0:
            raise AppError("字幕軌道不存在")
        item = p[key][i]
        undo = _update_item(item, op, patch, fields)
        p[key].sort(key=lambda it: it.get("start", 0))
        return {"op": f"{_SINGULAR[key]}.update", "id": item["id"], **undo}

    return handler


def _list_delete(key: str) -> Callable[[dict, dict], dict | None]:
    def handler(p: dict, op: dict) -> dict | None:
        i = _find(p[key], op.get("id"))
        if i < 0:
            return None
        item = p[key].pop(i)
        return {"op": f"{_SINGULAR[key]}.add", "item": item}

    return handler


_SINGULAR = {"audio": "audio", "captions": "caption", "cast": "cast", "overlays": "overlay",
             "captionStyles": "captionStyle", "adjustments": "adjust", "markers": "marker"}


def _caption_style_add(p: dict, op: dict) -> dict | None:
    style = clean_caption_style(op.get("item"))
    styles = p["captionStyles"]
    if _find(styles, style["id"]) >= 0:
        return None
    if len(styles) >= MAX_CAPTION_STYLES:
        raise AppError(f"一個專案最多 {MAX_CAPTION_STYLES} 個字幕樣式")
    index = op.get("index")
    index = len(styles) if index is None else max(0, min(len(styles), int(index)))
    styles.insert(index, style)
    return {"op": "captionStyle.delete", "id": style["id"]}


def _caption_style_delete(p: dict, op: dict) -> dict | None:
    """Captions using the style fall back to the project default; undo restores the links."""
    i = _find(p["captionStyles"], op.get("id"))
    if i < 0:
        return None
    style = p["captionStyles"].pop(i)
    restores = []
    for cap in p["captions"]:
        if cap.get("style") == style["id"]:
            cap["style"] = None
            restores.append({"op": "caption.update", "id": cap["id"], "patch": {"style": style["id"]}})
    if p["settings"].get("captionStyle") == style["id"]:
        p["settings"]["captionStyle"] = None
        restores.append({"op": "project.update", "patch": {"settings": {"captionStyle": style["id"]}}})
    add = {"op": "captionStyle.add", "item": style, "index": i}
    return {"op": "batch", "ops": [add, *restores]} if restores else add


def _layer_add(p: dict, op: dict) -> dict | None:
    layer = op.get("layer")
    if not isinstance(layer, dict) or not valid_id(layer.get("id")):
        raise AppError("軌道資料格式不正確")
    if _find(p["layers"], layer["id"]) >= 0:
        return None
    clean = {"id": layer["id"], "name": "", "hidden": False, "muted": False,
             **_clean_patch({k: v for k, v in layer.items() if k in LAYER_FIELDS}, LAYER_FIELDS)}
    index = op.get("index")
    index = len(p["layers"]) if index is None else max(0, min(len(p["layers"]), int(index)))
    p["layers"].insert(index, clean)
    return {"op": "layer.delete", "id": clean["id"]}


def _layer_update(p: dict, op: dict) -> dict | None:
    i = _find(p["layers"], op.get("id"))
    if i < 0:
        return None
    patch = _clean_patch(op.get("patch"), LAYER_FIELDS)
    layer = p["layers"][i]
    undo = _update_item(layer, op, patch, LAYER_FIELDS)
    return {"op": "layer.update", "id": layer["id"], **undo}


def _layer_delete(p: dict, op: dict) -> dict | None:
    """Removes the track and the clips on it; undo puts both back."""
    i = _find(p["layers"], op.get("id"))
    if i < 0:
        return None
    layer = p["layers"].pop(i)
    removed = [o for o in p["overlays"] if o.get("layer") == layer["id"]]
    p["overlays"] = [o for o in p["overlays"] if o.get("layer") != layer["id"]]
    return {"op": "batch", "ops": [{"op": "layer.add", "layer": layer, "index": i},
                                   *({"op": "overlay.add", "item": o} for o in removed)]}


def _cast_delete(p: dict, op: dict) -> dict | None:
    i = _find(p["cast"], op.get("id"))
    if i < 0:
        return None
    member = p["cast"].pop(i)
    restores = []
    for cap in p["captions"]:
        if cap.get("speaker") == member["id"]:
            cap["speaker"] = None
            restores.append({"op": "caption.update", "id": cap["id"], "patch": {"speaker": member["id"]}})
    if restores:
        return {"op": "batch", "ops": [{"op": "cast.add", "item": member}, *restores]}
    return {"op": "cast.add", "item": member}


def _media_add(p: dict, op: dict) -> dict | None:
    media = op.get("media")
    if not isinstance(media, dict) or not valid_id(media.get("id")):
        raise AppError("素材資料格式不正確")
    _check_files(media)
    if media["id"] in p["media"]:
        return None
    p["media"][media["id"]] = copy.deepcopy(media)
    return {"op": "media.delete", "id": media["id"]}


def _media_delete(p: dict, op: dict) -> dict | None:
    media = p["media"].pop(op.get("id"), None)
    if media is None:
        return None
    removed = [c for c in p["audio"] if c.get("mediaId") == media["id"]]
    p["audio"] = [c for c in p["audio"] if c.get("mediaId") != media["id"]]
    restores = [{"op": "audio.add", "item": c} for c in removed]
    overlays = [o for o in p["overlays"] if o.get("mediaId") == media["id"]]
    p["overlays"] = [o for o in p["overlays"] if o.get("mediaId") != media["id"]]
    restores += [{"op": "overlay.add", "item": o} for o in overlays]
    for shot in p["shots"]:
        if shot.get("ref") == media["id"]:
            shot["ref"] = None
            restores.append({"op": "shot.update", "id": shot["id"], "patch": {"ref": media["id"]}})
        _drop_extra_ref(shot, media["id"], restores)
    _drop_look_ref(p, media["id"], restores)
    if restores:
        return {"op": "batch", "ops": [{"op": "media.add", "media": media}, *restores]}
    return {"op": "media.add", "media": media}


def _media_update(p: dict, op: dict) -> dict | None:
    media = p["media"].get(op.get("id"))
    if media is None:
        return None
    fields = {"name": _str(200), "note": _str(1000), "favorite": _bool, "tags": _tags,
              "trashed": _bool}  # AU-14: a replaced voice-over in the project trash
    patch = _clean_patch(op.get("patch"), fields)
    undo = _update_item(media, op, patch, fields)
    return {"op": "media.update", "id": media["id"], **undo}


def _caption_track_add(p: dict, op: dict) -> dict | None:
    track = op.get("track")
    if not isinstance(track, dict) or not valid_id(track.get("id")):
        raise AppError("字幕軌道資料格式不正確")
    tracks = p.setdefault("captionTracks", [])
    if _find(tracks, track["id"]) >= 0:
        return None
    clean = {"id": track["id"], "name": "", "hidden": False,
             **_clean_patch({k: v for k, v in track.items() if k in CAPTION_TRACK_FIELDS}, CAPTION_TRACK_FIELDS)}
    index = op.get("index")
    index = len(tracks) if index is None else max(0, min(len(tracks), int(index)))
    tracks.insert(index, clean)
    return {"op": "captionTrack.delete", "id": clean["id"]}


def _caption_track_update(p: dict, op: dict) -> dict | None:
    tracks = p.get("captionTracks") or []
    i = _find(tracks, op.get("id"))
    if i < 0:
        return None
    patch = _clean_patch(op.get("patch"), CAPTION_TRACK_FIELDS)
    undo = _update_item(tracks[i], op, patch, CAPTION_TRACK_FIELDS)
    return {"op": "captionTrack.update", "id": tracks[i]["id"], **undo}


def _caption_track_delete(p: dict, op: dict) -> dict | None:
    """Removes the caption row and the captions on it; undo puts both back."""
    tracks = p.get("captionTracks") or []
    i = _find(tracks, op.get("id"))
    if i < 0:
        return None
    track = tracks.pop(i)
    removed = [c for c in p["captions"] if c.get("track") == track["id"]]
    p["captions"] = [c for c in p["captions"] if c.get("track") != track["id"]]
    return {"op": "batch", "ops": [{"op": "captionTrack.add", "track": track, "index": i},
                                   *({"op": "caption.add", "item": c} for c in removed)]}


HANDLERS: dict[str, Callable[..., dict | None]] = {
    "project.update": _project_update,
    "project.replace": _project_replace,
    "shot.delete": _shot_delete,
    "shot.update": _shot_update,
    "shot.move": _shot_move,
    "take.add": _take_add,
    "take.delete": _take_delete,
    "take.update": _take_update,
    "audio.add": _list_add("audio", AUDIO_FIELDS),
    "audio.update": _list_update("audio", AUDIO_FIELDS),
    "audio.delete": _list_delete("audio"),
    "caption.add": _list_add("captions", CAPTION_FIELDS),
    "caption.update": _list_update("captions", CAPTION_FIELDS),
    "caption.delete": _list_delete("captions"),
    "captionTrack.add": _caption_track_add,
    "captionTrack.update": _caption_track_update,
    "captionTrack.delete": _caption_track_delete,
    "cast.add": _list_add("cast", CAST_FIELDS),
    "cast.update": _list_update("cast", CAST_FIELDS),
    "cast.delete": _cast_delete,
    "captionStyle.add": _caption_style_add,
    "captionStyle.update": _list_update("captionStyles", CAPTION_STYLE_FIELDS),
    "captionStyle.delete": _caption_style_delete,
    "media.add": _media_add,
    "media.delete": _media_delete,
    "media.update": _media_update,
    "item.derive": _item_derive,
    "item.relink": _item_relink,
    "layer.add": _layer_add,
    "layer.update": _layer_update,
    "layer.delete": _layer_delete,
    "overlay.add": _list_add("overlays", OVERLAY_FIELDS),
    "overlay.update": _list_update("overlays", OVERLAY_FIELDS),
    "overlay.delete": _list_delete("overlays"),
    "adjust.add": _list_add("adjustments", ADJUST_FIELDS),
    "adjust.update": _list_update("adjustments", ADJUST_FIELDS),
    "adjust.delete": _list_delete("adjustments"),
    "marker.add": _list_add("markers", MARKER_FIELDS),
    "marker.update": _list_update("markers", MARKER_FIELDS),
    "marker.delete": _list_delete("markers"),
}


def apply_op(project: dict, op: dict, defaults: dict) -> list[dict]:
    """Apply one op (or a nested batch); return the inverse ops in application order."""
    if not isinstance(op, dict):
        raise AppError("操作格式不正確")
    name = op.get("op")
    if name in ("batch", "linked"):
        before = copy.deepcopy(project) if name == "linked" else None
        inverses: list[dict] = []
        for sub in op.get("ops") or []:
            inverses = apply_op(project, sub, defaults) + inverses
        if before is not None and inverses:
            for sub in follow_ops(before, project, shift=op.get("shift", True) is not False):
                inverses = apply_op(project, sub, defaults) + inverses
        return inverses
    if name == "shot.add":
        inv = _shot_add(project, op, defaults)
    else:
        handler = HANDLERS.get(name)
        if handler is None:
            raise AppError(f"不支援的操作：{name}")
        inv = handler(project, op)
    return [inv] if inv else []


def apply_ops(project: dict, ops: list[dict], defaults: dict) -> list[dict]:
    """Apply a batch; return the inverse batch (already reversed, ready to apply)."""
    if not isinstance(ops, list) or not ops:
        raise AppError("沒有要套用的操作")
    if len(ops) > MAX_OPS:
        # a long list (a big .srt) runs as nested batches, still one step to undo
        ops = [{"op": "batch", "ops": ops[i:i + MAX_OPS]} for i in range(0, len(ops), MAX_OPS)]
    inverse: list[dict] = []
    for op in ops:
        inverse = apply_op(project, op, defaults) + inverse
    return inverse
