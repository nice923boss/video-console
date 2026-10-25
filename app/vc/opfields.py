"""What project edit operations accept: id and value checks, the editable fields of each object, and the
builders of new shots and overlays. The operations themselves are in ops.py."""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from .grade import clean_grade
from .util import AppError
from .voiceclean import clean_channel, clean_denoise, clean_dereverb

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


MAX_TAGS = 20


def _tags(v: Any) -> list[str]:
    """Library tags: short words, trimmed, each once, in the order given."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise AppError("標籤格式不正確")
    tags = list(dict.fromkeys(t for t in (_str(30)(x).strip() for x in v) if t))
    if len(tags) > MAX_TAGS:
        raise AppError(f"每個素材最多 {MAX_TAGS} 個標籤")
    return tags


def _opt_id(v: Any) -> str | None:
    if v in (None, ""):
        return None
    if not valid_id(v):
        raise AppError("項目代號格式不正確")
    return v


def _audio_track(v: Any) -> str | None:
    """"voice" puts a clip on the voice-over rows; an id groups clips on one music track."""
    return "voice" if v == "voice" else _opt_id(v)


def _track_order(v: Any) -> list[str]:
    """The music tracks top to bottom, by key (see audio_track); tracks it leaves out go below in time order."""
    if v is None:  # undo of the first reorder
        return []
    if not isinstance(v, list):
        raise AppError("音軌順序格式不正確")
    return list(dict.fromkeys(_opt_id(x) for x in v if x))[:500]


def _row_keys(v: Any) -> list[str]:
    """Audio rows by key: a music track (see audio_track), "voice" for the 配音 rows, "source" for 原聲."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise AppError("音軌設定格式不正確")
    return list(dict.fromkeys(x if x in ("voice", "source") else _opt_id(x) for x in v if x))[:500]


def _opt_row_keys(v: Any) -> list[str] | None:
    return None if v is None else _row_keys(v)


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


CHROMA_DEFAULT = {"on": False, "color": "#00ff00", "similarity": 0.15, "blend": 0.1}


def _chroma(v: Any) -> dict:
    """Colour key of a picture-in-picture clip (GR-09), ffmpeg chromakey's settings. Missing keys take their
    defaults; switching it off keeps the colour and tolerances for the next time."""
    if not isinstance(v, dict):
        raise AppError("去背設定格式不正確")
    c = {**CHROMA_DEFAULT, **v}
    return {"on": _bool(c["on"]), "color": _hex(c["color"]), "similarity": _float(0.01, 1)(c["similarity"]),
            "blend": _float(0, 1)(c["blend"])}


MAX_MASKS = 8
MASK_DEFAULT = {"x": 0.4, "y": 0.35, "w": 0.2, "h": 0.3, "mode": "blur", "strength": 0.5}


def _masks(v: Any) -> list[dict]:
    """Blur or mosaic boxes over a shot's picture (GR-12): x, y, w, h are fractions of the output frame, the box
    kept inside it; strength 0.1..1 scales the blur radius or the mosaic block (export.py _mask_chains)."""
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > MAX_MASKS:
        raise AppError(f"遮罩格式不正確（每個鏡頭最多 {MAX_MASKS} 個）")
    out = []
    for m in v:
        if not isinstance(m, dict):
            raise AppError("遮罩格式不正確")
        m = {**MASK_DEFAULT, **m}
        w, h = _float(0.02, 1)(m["w"]), _float(0.02, 1)(m["h"])
        out.append({"x": _float(0, 1 - w)(m["x"]), "y": _float(0, 1 - h)(m["y"]), "w": w, "h": h,
                    "mode": _choice("blur", "mosaic")(m["mode"]), "strength": _float(0.1, 1)(m["strength"])})
    return out


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
MAX_OPS = 2000  # ops per batch level; longer lists are nested (apply_ops)
MAX_PARKED = 500


PIP_KEYS = ("x", "y", "w", "opacity", "volume", "chroma", "keyframes")


def _pip(v: Any) -> dict | None:
    """Where an upper-track clip sat before it became this shot (ED-16, lift.js dropOps): lifting the shot back
    restores it. Stored only; the picture lane still plays the shot full frame."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise AppError("上層位置格式不正確")
    return _clean_patch({k: x for k, x in v.items() if k in PIP_KEYS}, {k: OVERLAY_FIELDS[k] for k in PIP_KEYS})


def _parked(v: Any) -> dict:
    """Items a shot took along when it left the sequence (linked.py), timed from the shot's start."""
    lists = {"captions": CAPTION_FIELDS, "audio": AUDIO_FIELDS, "overlays": OVERLAY_FIELDS, "adjustments": ADJUST_FIELDS}
    if not isinstance(v, dict) or any(k not in lists for k in v):
        raise AppError("收起的項目格式不正確")
    out: dict[str, list] = {}
    for key, fields in lists.items():
        items = v.get(key) or []
        if not isinstance(items, list) or len(items) > MAX_PARKED:
            raise AppError("收起的項目格式不正確")
        for item in items:
            if not isinstance(item, dict) or not valid_id(item.get("id")):
                raise AppError("收起的項目格式不正確")
            clean = {"id": item["id"], **_clean_patch({k: x for k, x in item.items() if k in fields}, fields)}
            out.setdefault(key, []).append({**clean, **{k: item[k] for k in ("mediaId", "file") if k in item}})
    return out


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


MAX_REVIEW = 200


def _review(v: Any) -> list[dict]:
    """Client review comments (SB-13, review.py): [{by, at, text}], oldest first; one with no text is dropped."""
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > MAX_REVIEW:
        raise AppError(f"審稿意見格式不正確（最多 {MAX_REVIEW} 則）")
    out = []
    for c in v:
        if not isinstance(c, dict):
            raise AppError("審稿意見格式不正確")
        text = _str(2000)(c.get("text")).strip()
        if text:
            out.append({"by": _str(60)(c.get("by")).strip(), "at": _str(40)(c.get("at")), "text": text})
    return out


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
    "grade": clean_grade,  # colour grade of this shot's picture alone, under any adjustment layer (GR-01)
    "masks": _masks,  # blur or mosaic boxes over the shot's picture, to hide a face or a logo (GR-12)
    "notes": _str(4000),
    "review": _review,  # comments a client wrote on the review page (review.py)
    "color": _opt_str(20),
    "imageWorkflow": _str(80),
    "videoWorkflow": _str(80),
    "imageParams": _params,
    "videoParams": _params,
    "parked": _parked,  # set while the shot is out of the sequence: its captions, sounds and clips (linked.py)
    "pip": _pip,  # place, size, opacity, keying and volume it had as an upper-track clip (ED-16)
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
    "fixed": _bool,  # stays at its time when linked shot edits move everything else
    "speed": _float(0.25, 4),  # set on the detached sound of a sped-up shot: source seconds per timeline second
    "speech": _bool,  # counts as voice: music on the 背景音樂 rows dips under it (duck.py)
    "denoise": clean_denoise,  # noise reduction learned from a sample of the clip (voiceclean.py)
    "dereverb": clean_dereverb,  # the classic echo remover
    "channel": clean_channel,  # the one channel of its file the clip plays; None: all of them
}
CAPTION_FIELDS = {"start": _float(0, 36000), "end": _float(0, 36000), "text": _str(500), "position": _str(10),
                  "speaker": _opt_id,
                  "style": _opt_id,  # a project caption style; None: the project default
                  "x": _opt_float(0, 1),  # dragged to this point (fractions of the frame); None: the style decides
                  "y": _opt_float(0, 1),
                  "shot": _opt_id,  # the storyboard shot showing this text; timing stays the caption's own
                  "sttShot": _opt_id,  # added by speech-to-subtitle from this shot; a rerun replaces it
                  "title": _bool,  # a title card (GR-10): burnt in like the rest, left out of the .srt
                  "track": _opt_id,  # one of project.captionTracks; None: the main 字幕 row
                  "pair": _opt_id,  # the caption this one translates (bilingual import); speech alignment times both
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
    "highlight": _bool,  # CP-13: each character turns highlightColor as the caption's voice-over says it
    "highlightColor": _hex,
}
# The look captions had before styles existed.
DEFAULT_CAPTION_STYLE = {
    "name": "預設", "font": "Microsoft JhengHei", "size": 52, "color": "#ffffff", "opacity": 1.0, "bold": True,
    "italic": False, "spacing": 0.0, "outline": True, "outlineColor": "#000000", "outlineWidth": 4.0,
    "shadow": True, "shadowColor": "#000000", "shadowOpacity": 0.4, "shadowDistance": 1.0, "shadowBlur": 0.0,
    "box": False, "boxColor": "#000000", "boxOpacity": 0.6, "boxPadding": 12.0, "align": "center",
    "marginV": 65.0, "marginH": 80.0, "x": None, "y": None, "highlight": False, "highlightColor": "#ffd400",
}
MAX_CAPTION_STYLES = 50
CAST_FIELDS = {"name": _str(40), "voice": _str(200), "voiceName": _str(80), "speed": _float(0.5, 2.0),
               "volume": _float(0, 4), "engine": _str(20), "style": _str(40), "model": _str(20),
               "look": _str(1000), "lookRef": _opt_id}  # look: appearance text for picture prompts (SB-11)
SETTINGS_FIELDS = {
    "width": _int(64, 4096),
    "height": _int(64, 4096),
    "fps": _fps,
    "aspect": _str(10),
    "fadeIn": _float(0, 10),
    "fadeOut": _float(0, 10),
    "captionStyle": _opt_id,  # default caption style; None: DEFAULT_CAPTION_STYLE
    "fit": _choice("contain", "cover"),  # framing of clips whose aspect differs; preview and export share it
    "gradeCaptions": _bool,  # captions are burnt in before the adjustment layers, so they get graded too
    "duckDb": _opt_float(0, 24),  # how far the 背景音樂 rows dip under voice; None: duck.DEFAULT_DB
}
STYLE_FIELDS = {"prompt": _str(4000), "negative": _str(4000), "preset": _str(40)}
# Switches on the fixed timeline rows; picture-in-picture tracks carry their own in project["layers"].
TRACK_FIELDS = {"videoHidden": _bool, "videoMuted": _bool, "captionsHidden": _bool, "voiceMuted": _bool,
                "musicMuted": _bool, "adjustHidden": _bool, "musicOrder": _track_order,
                "speechRows": _opt_row_keys,  # rows whose every clip is voice; None: the 配音 rows (duck.py)
                "bgmRows": _row_keys}  # music tracks that dip under voice
LAYER_FIELDS = {"name": _str(40), "hidden": _bool, "muted": _bool}
CAPTION_TRACK_FIELDS = {"name": _str(40), "hidden": _bool}
OVERLAY_FIELDS = {
    "name": _str(120),
    "layer": _opt_id,
    "start": _float(0, 36000),
    "in": _float(0, 36000),  # seconds into a video file; pictures ignore it
    "length": _float(0.1, 36000),
    "x": _float(-4, 4),  # left edge as a fraction of the frame width
    "y": _float(-4, 4),  # top edge as a fraction of the frame height
    "w": _float(0.05, 4),  # width as a fraction of the frame width; height follows the file's aspect; past the frame is cropped
    "opacity": _float(0, 1),
    "volume": _float(0, 4),
    "disabled": _bool,
    "chroma": _chroma,
    "keyframes": _keyframes({"x": _float(-4, 4), "y": _float(-4, 4), "w": _float(0.05, 4), "opacity": _float(0, 1),
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
