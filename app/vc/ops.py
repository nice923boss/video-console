"""Project edit operations.

Every operation is idempotent (re-applying it is harmless) and returns its inverse so the
store can offer undo/redo. Operations mutate the working copy passed in; the store commits
the copy only when the whole batch succeeds.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from .grade import clean_grade
from .linked import follow_ops
from .util import AppError

_ID_RE = re.compile(r"^[a-z]{1,3}_[a-z0-9]{6,24}$")
TRANSITIONS = ("cut", "crossfade", "fade_black", "fade_white", "wipe_left", "slide_left")


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _str(limit: int) -> Callable[[Any], str]:
    def conv(v: Any) -> str:
        if v is None:
            return ""
        if not isinstance(v, (str, int, float)):
            raise AppError("文字欄位格式不正確")
        return str(v)[:limit]

    return conv


def _opt_str(limit: int) -> Callable[[Any], str | None]:
    def conv(v: Any) -> str | None:
        return None if v in (None, "") else _str(limit)(v)

    return conv


def _float(lo: float, hi: float) -> Callable[[Any], float]:
    def conv(v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise AppError("數值欄位格式不正確") from None
        if f != f:  # NaN
            raise AppError("數值欄位格式不正確")
        return round(max(lo, min(hi, f)), 4)

    return conv


def _opt_float(lo: float, hi: float) -> Callable[[Any], float | None]:
    def conv(v: Any) -> float | None:
        return None if v in (None, "") else _float(lo, hi)(v)

    return conv


def _int(lo: int, hi: int) -> Callable[[Any], int]:
    def conv(v: Any) -> int:
        try:
            i = int(v)
        except (TypeError, ValueError):
            raise AppError("整數欄位格式不正確") from None
        return max(lo, min(hi, i))

    return conv


def _fps(v: Any) -> int | float:
    """Frame rate 1~60: whole rates stay int, 29.97 keeps its decimals."""
    f = _float(1, 60)(v)
    return int(f) if f.is_integer() else f


def _bool(v: Any) -> bool:
    return bool(v)


def _opt_id(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if not valid_id(v):
        raise AppError("項目代號格式不正確")
    return v


def _audio_track(v: Any) -> str | None:
    """"voice" puts a clip on the voice-over rows; an id groups clips on one music track."""
    return "voice" if v == "voice" else _opt_id(v)


MAX_REFS = 10  # reference pictures one job may read (FLUX3-Klein): the shot's ref plus nine extras


def _extra_refs(v: Any) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise AppError("額外參考圖格式不正確")
    ids = list(dict.fromkeys(_opt_id(x) for x in v if x))
    if len(ids) > MAX_REFS - 1:
        raise AppError(f"參考圖最多 {MAX_REFS} 張")
    return ids


def _hex(v: Any) -> str:
    if not isinstance(v, str) or not re.match(r"^#[0-9a-fA-F]{6}$", v):
        raise AppError("顏色格式不正確，例如 #ffffff")
    return v.lower()


def _choice(*values: str) -> Callable[[Any], str]:
    def conv(v: Any) -> str:
        if v not in values:
            raise AppError("選項不正確")
        return v

    return conv


def _font(v: Any) -> str:
    # ASS style lines are comma separated; braces would open an override block
    return re.sub(r"[,{}\\\r\n]", "", _str(80)(v)).strip()


def _params(v: Any) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict) or len(v) > 40:
        raise AppError("參數格式不正確")
    out = {}
    for k, val in v.items():
        if not isinstance(k, str) or len(k) > 40:
            raise AppError("參數名稱格式不正確")
        if isinstance(val, (str, int, float, bool)) or val is None:
            out[k] = val if not isinstance(val, str) else val[:4000]
        else:
            raise AppError("參數值只能是文字、數字或布林")
    return out


def _transition(v: Any) -> dict:
    if not isinstance(v, dict):
        raise AppError("轉場格式不正確")
    kind = v.get("type", "cut")
    if kind not in TRANSITIONS:
        raise AppError("不支援的轉場類型")
    return {"type": kind, "duration": _float(0.1, 5.0)(v.get("duration", 0.5))}


MAX_SEED = 2**53 - 1
MAX_KEYFRAMES = 200


def _keyframes(fields: dict[str, Callable[[Any], Any]]) -> Callable[[Any], list]:
    """[{t, <field>: value, ...}]: t is seconds into the clip; later values win on a repeated t."""

    def conv(v: Any) -> list:
        if v is None:
            return []
        if not isinstance(v, list) or len(v) > MAX_KEYFRAMES:
            raise AppError("關鍵幀格式不正確")
        out: dict[float, dict] = {}
        for k in v:
            if not isinstance(k, dict):
                raise AppError("關鍵幀格式不正確")
            t = round(_float(0, 36000)(k.get("t")), 3)
            out[t] = {"t": t, **_clean_patch({f: v for f, v in k.items() if f != "t"}, fields)}
        return [out[t] for t in sorted(out)]

    return conv


SHOT_FIELDS: dict[str, Callable[[Any], Any]] = {
    "name": _str(120),
    "prompt": _str(8000),
    "imagePrompt": _str(8000),  # sent as-is for pictures; empty: description plus the project style
    "negative": _str(4000),
    "motion": _str(4000),
    "camera": _str(200),
    "duration": _float(0.2, 600),
    "videoDuration": _opt_float(0.5, 60),  # seconds asked of the video model; None: the length nearest "duration"
    "videoUseImage": _bool,  # video jobs start from the shot picture; False: generate from the text
    "seed": _int(0, MAX_SEED),
    "seedLocked": _bool,
    "route": _str(12),
    "inSequence": _bool,
    "activeImage": _opt_id,
    "activeVideo": _opt_id,
    "ref": _opt_id,
    "refStrength": _float(0.05, 1.0),
    "extraRefs": _extra_refs,  # more reference pictures after "ref", for templates that read several
    "trimIn": _float(0, 36000),
    "trimOut": _opt_float(0, 36000),
    "transition": _transition,
    "disabled": _bool,  # kept on the timeline but shown as black and silent
    "speed": _float(0.25, 4),  # playback speed of a video shot; its length on the timeline is (out - in) / speed
    "audioDetached": _bool,  # the shot's own sound plays from an audio clip instead (see AUDIO_FIELDS takeId)
    "notes": _str(4000),
    "color": _opt_str(20),
    "imageWorkflow": _str(80),
    "videoWorkflow": _str(80),
    "imageParams": _params,
    "videoParams": _params,
}
TAKE_FIELDS = {"favorite": _bool, "rating": _int(0, 5), "note": _str(1000), "label": _str(120)}
AUDIO_FIELDS = {
    "name": _str(120),
    "start": _float(0, 36000),
    "in": _float(0, 36000),
    "out": _opt_float(0, 36000),
    "volume": _float(0, 4),
    "fadeIn": _float(0, 30),
    "fadeOut": _float(0, 30),
    "muted": _bool,
    "captionId": _opt_id,  # set on voice-over clips: the caption they speak
    "shotId": _opt_id,  # detached sound of this shot ...
    "takeId": _opt_id,  # ... read from this take's file instead of a library item
    "track": _audio_track,  # None: a music track of its own; an id: the music track it shares; "voice": voice rows
    "keyframes": _keyframes({"volume": _float(0, 4)}),
}
CAPTION_FIELDS = {"start": _float(0, 36000), "end": _float(0, 36000), "text": _str(500), "position": _str(10),
                  "speaker": _opt_id,
                  "style": _opt_id,  # a project caption style; None: the project default
                  "x": _opt_float(0, 1),  # dragged to this point (fractions of the frame); None: the style decides
                  "y": _opt_float(0, 1),
                  "shot": _opt_id,  # the storyboard shot showing this text; timing stays the caption's own
                  "disabled": _bool}
# Sizes and distances are pixels on a 1080-high frame, as in the burnt-in ASS script.
CAPTION_STYLE_FIELDS = {
    "name": _str(40),
    "font": _font,
    "size": _int(16, 200),
    "color": _hex,
    "opacity": _float(0, 1),
    "bold": _bool,
    "italic": _bool,
    "spacing": _float(-10, 40),
    "outline": _bool,
    "outlineColor": _hex,
    "outlineWidth": _float(0, 20),
    "shadow": _bool,
    "shadowColor": _hex,
    "shadowOpacity": _float(0, 1),
    "shadowDistance": _float(0, 30),
    "shadowBlur": _float(0, 20),
    "box": _bool,
    "boxColor": _hex,
    "boxOpacity": _float(0, 1),
    "boxPadding": _float(0, 60),
    "align": _choice("left", "center", "right"),
    "marginV": _float(0, 500),
    "marginH": _float(0, 600),
    "x": _opt_float(0, 1),  # dragged anchor for every caption of the style; None: the caption's top/middle/bottom row
    "y": _opt_float(0, 1),
}
# The look captions had before styles existed.
DEFAULT_CAPTION_STYLE = {
    "name": "預設", "font": "Microsoft JhengHei", "size": 52, "color": "#ffffff", "opacity": 1.0, "bold": True,
    "italic": False, "spacing": 0.0, "outline": True, "outlineColor": "#000000", "outlineWidth": 4.0,
    "shadow": True, "shadowColor": "#000000", "shadowOpacity": 0.4, "shadowDistance": 1.0, "shadowBlur": 0.0,
    "box": False, "boxColor": "#000000", "boxOpacity": 0.6, "boxPadding": 12.0, "align": "center",
    "marginV": 65.0, "marginH": 80.0, "x": None, "y": None,
}
MAX_CAPTION_STYLES = 50
CAST_FIELDS = {"name": _str(40), "voice": _str(200), "voiceName": _str(80), "speed": _float(0.5, 2.0),
               "volume": _float(0, 4), "engine": _str(20), "style": _str(40), "model": _str(20)}
SETTINGS_FIELDS = {
    "width": _int(64, 4096),
    "height": _int(64, 4096),
    "fps": _fps,
    "aspect": _str(10),
    "fadeIn": _float(0, 10),
    "fadeOut": _float(0, 10),
    "captionStyle": _opt_id,  # default caption style; None: DEFAULT_CAPTION_STYLE
}
STYLE_FIELDS = {"prompt": _str(4000), "negative": _str(4000), "preset": _str(40)}
# Switches on the fixed timeline rows; picture-in-picture tracks carry their own in project["layers"].
TRACK_FIELDS = {"videoHidden": _bool, "videoMuted": _bool, "captionsHidden": _bool, "voiceMuted": _bool,
                "musicMuted": _bool, "adjustHidden": _bool}
LAYER_FIELDS = {"name": _str(40), "hidden": _bool, "muted": _bool}
OVERLAY_FIELDS = {
    "name": _str(120),
    "layer": _opt_id,
    "start": _float(0, 36000),
    "in": _float(0, 36000),  # seconds into a video file; pictures ignore it
    "length": _float(0.1, 36000),
    "x": _float(-1, 1),  # left edge as a fraction of the frame width
    "y": _float(-1, 1),  # top edge as a fraction of the frame height
    "w": _float(0.05, 1),  # width as a fraction of the frame width; height follows the file's aspect
    "opacity": _float(0, 1),
    "volume": _float(0, 4),
    "disabled": _bool,
    "keyframes": _keyframes({"x": _float(-1, 1), "y": _float(-1, 1), "w": _float(0.05, 1), "opacity": _float(0, 1),
                             "volume": _float(0, 4)}),
}
# Adjustment layers: a span of the timeline whose grade applies to every picture underneath it.
ADJUST_FIELDS = {
    "name": _str(40),
    "start": _float(0, 36000),
    "length": _float(0.1, 36000),
    "hidden": _bool,
    "grade": clean_grade,
    "keyframes": _keyframes({"grade": clean_grade}),
}
MARKER_COLORS = ("blue", "red", "orange", "yellow", "green", "purple")
MARKER_FIELDS = {"start": _float(0, 36000), "color": _choice(*MARKER_COLORS), "name": _str(80)}


def _clean_patch(patch: Any, fields: dict[str, Callable[[Any], Any]]) -> dict:
    if not isinstance(patch, dict):
        raise AppError("修改內容格式不正確")
    out = {}
    for key, value in patch.items():
        conv = fields.get(key)
        if conv is None:
            raise AppError(f"不允許修改的欄位：{key}")
        out[key] = conv(value)
    return out


def _update_item(item: dict, op: dict, patch: dict, fields: dict) -> dict:
    """Apply patch and drop op["unset"]; returns the patch/unset pair that undoes it.

    Fields the item did not have go into the undo's unset list: writing None back would fail
    validators such as _float."""
    unset = op.get("unset") or []
    if not isinstance(unset, list) or any(k not in fields for k in unset):
        raise AppError("修改內容格式不正確")
    keys = [*patch, *unset]
    old = {k: copy.deepcopy(item[k]) for k in keys if k in item}
    gone = [k for k in dict.fromkeys(keys) if k not in item]
    item.update(patch)
    for k in unset:
        item.pop(k, None)
    return {"patch": old, "unset": gone} if gone else {"patch": old}


def snap16(v: int) -> int:
    return max(64, int(round(v / 16.0)) * 16)


def snap8(v: int) -> int:
    """A project frame side: multiples of 8 keep 1920x1080 exact (snap16 would make it 1088)."""
    return max(64, int(round(v / 8.0)) * 8)


def _find(items: list[dict], item_id: str) -> int:
    for i, it in enumerate(items):
        if it.get("id") == item_id:
            return i
    return -1


def clean_caption_style(item: Any) -> dict:
    """A full caption style (project styles and the app-wide library); missing fields take the defaults."""
    if not isinstance(item, dict) or not valid_id(item.get("id")):
        raise AppError("字幕樣式資料格式不正確")
    patch = _clean_patch({k: v for k, v in item.items() if k in CAPTION_STYLE_FIELDS}, CAPTION_STYLE_FIELDS)
    style = {"id": item["id"], **DEFAULT_CAPTION_STYLE, **patch}
    style["font"] = style["font"] or DEFAULT_CAPTION_STYLE["font"]
    style["name"] = style["name"].strip() or "未命名樣式"
    return style


def clean_shot(shot: Any, defaults: dict | None = None) -> dict:
    """Validate a full shot object (used by shot.add and script import)."""
    if not isinstance(shot, dict) or not valid_id(shot.get("id")):
        raise AppError("鏡頭資料格式不正確")
    base = blank_shot(shot["id"], defaults or {})
    base.update(_clean_patch({k: v for k, v in shot.items() if k in SHOT_FIELDS}, SHOT_FIELDS))
    return base


def blank_shot(shot_id: str, defaults: dict) -> dict:
    return {
        "id": shot_id,
        "name": "",
        "prompt": "",
        "imagePrompt": "",
        "negative": "",
        "motion": "",
        "camera": "",
        "duration": float(defaults.get("shotDuration", 5.0)),
        "videoDuration": None,
        "videoUseImage": True,
        "seed": 0,
        "seedLocked": False,
        "route": defaults.get("route", "A"),
        "inSequence": True,
        "activeImage": None,
        "activeVideo": None,
        "ref": None,
        "refStrength": 0.6,
        "extraRefs": [],
        "trimIn": 0.0,
        "trimOut": None,
        "transition": {"type": "cut", "duration": 0.5},
        "notes": "",
        "color": None,
        "imageWorkflow": "",
        "videoWorkflow": "",
        "imageParams": {},
        "videoParams": {},
    }


def fit_frame(media: dict, frame_w: int, frame_h: int) -> dict:
    """The largest size that shows the whole picture, centered (x, y, w as fractions of the frame)."""
    aspect = (media.get("width") or 16) / (media.get("height") or 9)
    full = frame_w / frame_h / aspect
    w = 1 / full if full > 1 else 1.0
    h = min(1.0, full)
    return {"x": round((1 - w) / 2, 4), "y": round((1 - h) / 2, 4), "w": round(w, 4)}


def new_overlay(overlay_id: str, media: dict, layer_id: str, start: float, frame_w: int, frame_h: int) -> dict:
    """A clip on an upper track that fills the frame."""
    video = media.get("kind") == "video" and media.get("duration")
    return {"id": overlay_id, "mediaId": media["id"], "layer": layer_id, "name": str(media.get("name") or "")[:120],
            "start": round(max(0.0, float(start)), 3), "in": 0.0,
            "length": round(max(0.1, float(media["duration"])), 3) if video else 5.0,
            **fit_frame(media, frame_w, frame_h), "opacity": 1.0, "volume": 1.0}


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
    if restores:
        return {"op": "batch", "ops": [{"op": "take.add", "take": take}, *restores]}
    return {"op": "take.add", "take": take}


def _drop_extra_ref(shot: dict, item_id: str, restores: list[dict]) -> None:
    extras = shot.get("extraRefs") or []
    if item_id in extras:
        shot["extraRefs"] = [x for x in extras if x != item_id]
        restores.append({"op": "shot.update", "id": shot["id"], "patch": {"extraRefs": extras}})


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
    if restores:
        return {"op": "batch", "ops": [{"op": "media.add", "media": media}, *restores]}
    return {"op": "media.add", "media": media}


def _media_update(p: dict, op: dict) -> dict | None:
    media = p["media"].get(op.get("id"))
    if media is None:
        return None
    fields = {"name": _str(200), "note": _str(1000), "favorite": _bool}
    patch = _clean_patch(op.get("patch"), fields)
    undo = _update_item(media, op, patch, fields)
    return {"op": "media.update", "id": media["id"], **undo}


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
    "cast.add": _list_add("cast", CAST_FIELDS),
    "cast.update": _list_update("cast", CAST_FIELDS),
    "cast.delete": _cast_delete,
    "captionStyle.add": _caption_style_add,
    "captionStyle.update": _list_update("captionStyles", CAPTION_STYLE_FIELDS),
    "captionStyle.delete": _caption_style_delete,
    "media.add": _media_add,
    "media.delete": _media_delete,
    "media.update": _media_update,
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
            for sub in follow_ops(before, project):
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
    if len(ops) > 2000:
        raise AppError("一次操作太多項目")
    inverse: list[dict] = []
    for op in ops:
        inverse = apply_op(project, op, defaults) + inverse
    return inverse
