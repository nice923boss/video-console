"""Server versions of edit actions whose logic first lived only in the browser, so a script or the AI assistant
can run them through POST /api/projects/{pid}/edit like the other commands. Each entry of CMDS takes
(project, data) and returns what edits.plan returns: {ops, label, ...}.

The ports themselves sit beside their JS sources' names (declip.py, placing.py, ripple.py, title_templates.py,
glossary.py); this module checks the command's parameters and words the errors. tests/test_twins.py runs the JS
and the Python on the same project and compares the ops.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import edits
from .declip import declip_keys, play_gain, with_dips
from .glossary import fix_hits, glossary_hits, parse_glossary
from .jscompat import r3, value_at
from .opfields import MAX_KEYFRAMES
from .placing import drop_ops, fit_gap, layer_spans, lift_ops, place_ops, shot_index_at, take_from_media
from .ripple import LISTS, ripple_ops
from .sequence import audio_media
from .title_templates import LEN_RANGE, TEMPLATES, template, title_ops
from .util import AppError, new_id


# ---------------------------------------------------------------- what the commands read outside the project
@dataclass(frozen=True)
class Sources:
    """A media file's wave JSON (project id, project-relative path; None when unreadable) and the settings
    glossary text. api/twins.py binds the server's own; tests bind theirs."""
    wave: Callable[[str, str], Any] = lambda pid, rel: None
    glossary: Callable[[], str] = lambda: ""


_sources = Sources()


def bind(sources: Sources) -> None:
    global _sources
    _sources = sources


def glossary_text() -> str:
    """The settings glossary (字幕用語詞庫) as the bound source gives it."""
    return str(_sources.glossary() or "")


def _need_id(data: dict, what: str) -> str:
    v = edits._id(data)
    if not v:
        raise AppError(f"{data.get('cmd')} 要給{what} id")
    return v


def _clip(project: dict, sid: str) -> dict:
    clip = next((c for c in edits._clips(project) if c["shot"]["id"] == sid), None)
    if not clip:
        raise AppError("找不到這個鏡頭，或它不在剪輯序列裡")
    return clip


def _layer(project: dict, layer: Any) -> str | None:
    """None or "new" as given, else an existing picture layer's id."""
    if layer in (None, "", "new"):
        return layer or None
    if not any(x.get("id") == layer for x in project.get("layers") or []):
        raise AppError("找不到這個畫面軌，layer 要是畫面軌 id 或 new")
    return str(layer)


# ---------------------------------------------------------------- declip (壓低爆音)
def _wave(project: dict, media: dict) -> Any:
    rel = media.get("wave")
    return _sources.wave(project["id"], rel) if rel else None


def _declip_audio(project: dict, aid: str) -> dict:
    """The 壓低爆音 button of an audio clip (components/edit-props.js): dips multiplied into its volume keyframes."""
    item = next((a for a in project.get("audio") or [] if a.get("id") == aid), None)
    if not item:
        raise AppError("找不到這個音訊片段")
    media = audio_media(project, item)
    if not media:
        raise AppError("找不到這條音軌的音訊檔，可能已從素材庫刪除")
    length = edits.audio_length(project, item)
    room = MAX_KEYFRAMES - sum(1 for k in item.get("keyframes") or [] if "volume" in k)
    found = declip_keys(_wave(project, media), float(item.get("in") or 0), length, float(item.get("speed") or 1),
                        lambda t: play_gain(item, t, length), room)
    if found is None:
        raise AppError("這段音訊的波形沒有音量資料，無法偵測爆音")
    if not found["spans"]:
        raise AppError("這段音訊沒有爆音")
    keys = with_dips(item, found["keys"], length)
    return {"ops": [{"op": "audio.update", "id": aid, "patch": {"keyframes": keys}}], "label": "壓低爆音",
            "spans": len(found["spans"])}


def _declip_shot(project: dict, sid: str) -> dict:
    """The 壓低爆音 button of a video shot's own sound (declipShot in components/insp-edit.js): the version's
    volume keys, in file seconds, worked out afresh at the shot volume."""
    clip = _clip(project, sid)
    shot, vid = clip["shot"], clip["take"]
    if clip["source"] != "video" or not vid:
        raise AppError("只有影片鏡頭有原聲可以壓低爆音")
    if not vid.get("hasAudio"):
        raise AppError("這段影片沒有聲音")
    if shot.get("audioDetached"):
        raise AppError("這個鏡頭的原聲已分離到音訊軌，請用 type audio 指定分離出的音訊片段")
    speed = clip["speed"] or 1
    base = float(1 if shot.get("volume") is None else shot["volume"])
    gains = {"keyframes": vid.get("gainKeys") or []}
    found = declip_keys(_wave(project, vid), clip["in"], clip["length"], speed,
                        lambda t: base * value_at(gains, "volume", clip["in"] + t * speed, 1))
    if found is None:
        raise AppError("這段影片的波形沒有音量資料，無法偵測爆音")
    if not found["spans"] and not vid.get("volumeKeys"):
        raise AppError("這段原聲沒有爆音")
    keys = [{"t": r3(clip["in"] + k["t"] * speed), "volume": k["volume"]} for k in found["keys"]]
    return {"ops": [{"op": "take.update", "id": vid["id"], "patch": {"volumeKeys": keys}}], "label": "壓低爆音",
            "spans": len(found["spans"])}


def _declip(project: dict, data: dict) -> dict:
    kind = str(data.get("type") or "shot")
    if kind == "audio":
        return _declip_audio(project, _need_id(data, "音訊片段"))
    if kind == "shot":
        return _declip_shot(project, _need_id(data, "鏡頭"))
    raise AppError("declip 的 type 要是 audio 或 shot")


# ---------------------------------------------------------------- lift / drop (components/tl-tracks.js)
def _lift(project: dict, data: dict) -> dict:
    clip = _clip(project, _need_id(data, "鏡頭"))
    want = edits._num(data, "start", required=False)
    layer = _layer(project, data.get("layer"))
    if layer in (None, "new"):
        target, start = "new-layer", clip["start"] if want is None else want
    else:
        wanted = r3(max(0, clip["start"] if want is None else want))
        start = fit_gap(wanted, clip["length"], layer_spans(project.get("overlays"), layer))
        if start is None:
            raise AppError("這條畫面軌在那個時間沒有夠長的空檔，請換個 start，或用 layer new 放到新的畫面軌")
        target = "layer:" + layer
    res = lift_ops(project, clip, target, start)
    return {"ops": res["ops"], "label": "移到上層軌", "id": res["id"], "link": False}


def _drop(project: dict, data: dict) -> dict:
    oid = _need_id(data, "上方片段")
    item = next((o for o in project.get("overlays") or [] if o.get("id") == oid), None)
    if not item:
        raise AppError("找不到這個上方片段")
    media = (project.get("media") or {}).get(item.get("mediaId"))
    if not media:
        raise AppError("這個片段的素材已從素材庫刪除，無法移到畫面軌")
    index, count = data.get("index"), len(project["shots"])
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= count:
        raise AppError(f"index 要是 0 到 {count} 的整數（新鏡頭的位置）")
    res = drop_ops(project, item, media, index, take_from_media(media, new_id("s")))
    return {"ops": res["ops"], "label": "移到畫面軌", "id": res["shot"]["id"]}


# ---------------------------------------------------------------- rippleDelete (lib/ripple.js)
def _ripple_delete(project: dict, data: dict) -> dict:
    kind = str(data.get("type") or "")
    if kind not in LISTS:
        raise AppError("rippleDelete 的 type 要是 caption、audio、overlay 或 adjust（刪掉鏡頭時，後面的鏡頭本來就會往前補）")
    ops = ripple_ops(project, kind, _need_id(data, "片段"))
    if ops is None:
        raise AppError("找不到這個片段")
    return {"ops": ops, "label": "漣漪刪除"}


# ---------------------------------------------------------------- title (dialogs/title-dialog.js)
def _title(project: dict, data: dict) -> dict:
    tpl = template(data.get("template"))
    if not tpl:
        raise AppError("找不到這個標題範本，可用：" + "、".join(t["id"] for t in TEMPLATES))
    raw = data.get("values") or {}
    if not isinstance(raw, dict) or not all(isinstance(v, (str, dict)) for v in raw.values()):
        raise AppError("values 要是 {欄位: 文字} 或 {欄位: {text, color, boxColor}}")
    values = {k: {"text": v} if isinstance(v, str) else v for k, v in raw.items()}
    start = edits._num(data, "start")
    if start < 0:
        raise AppError("start 不能小於 0")
    length = edits._num(data, "len", required=False)
    if length is not None and not LEN_RANGE[0] <= length <= LEN_RANGE[1]:
        raise AppError("len 要在 1 到 30 秒之間")
    res = title_ops(project, tpl, values, start, length)
    return {"ops": res["ops"], "label": f"加入標題字卡：{tpl['name']}", "id": res["first"]}


# ---------------------------------------------------------------- glossaryFix (components/edit-props.js 全部改正)
def _glossary_fix(project: dict, data: dict) -> dict:
    entries = parse_glossary(glossary_text())
    if not entries:
        raise AppError("字幕用語詞庫是空的，請先到設定填寫")
    caps = project.get("captions") or []
    ids = data.get("ids")
    if ids is not None:
        if not isinstance(ids, list):
            raise AppError("ids 要是字幕 id 的清單")
        known = {c.get("id") for c in caps}
        unknown = [str(i) for i in ids if i not in known]
        if unknown:
            raise AppError("找不到這些字幕：" + "、".join(unknown[:5]))
        caps = [c for c in caps if c.get("id") in set(ids)]
    ops = []
    for c in caps:
        hits = glossary_hits(c.get("text"), entries)
        fixed = fix_hits(c.get("text"), hits)
        if hits and fixed != c.get("text"):
            ops.append({"op": "caption.update", "id": c["id"], "patch": {"text": fixed}})
    if not ops:
        raise AppError("字幕裡沒有詞庫列出的錯字")
    return {"ops": ops, "label": "改正字幕錯字"}


# ---------------------------------------------------------------- place (lib/media-actions.js placeMedia)
def _place(project: dict, data: dict) -> dict:
    ids = data.get("media")
    ids = [ids] if isinstance(ids, str) else ids
    if not isinstance(ids, list) or not ids:
        raise AppError("media 要是素材 id，或素材 id 的清單")
    library = project.get("media") or {}
    missing = [str(i) for i in ids if not isinstance(i, str) or i not in library]
    if missing:
        raise AppError("素材庫裡找不到：" + "、".join(missing[:5]))
    at = edits._num(data, "at", required=False)
    if at is not None and at < 0:
        raise AppError("at 不能小於 0")
    layer = _layer(project, data.get("layer"))
    track = data.get("track") or None
    if track is not None and not isinstance(track, str):
        raise AppError("track 要是音軌名稱")
    index = len(project["shots"]) if at is None else shot_index_at(project, edits._clips(project), at)
    ops = place_ops(project, [library[i] for i in ids], index, at or 0.0, layer, track)
    if not ops:
        raise AppError("這些素材都不能放上時間軸，只能放圖片、影片與音訊")
    first = next(o.get("item") or o["shot"] for o in ops if o["op"] in ("overlay.add", "audio.add", "shot.add"))
    return {"ops": ops, "label": "放上時間軸", "id": first["id"]}


# ---------------------------------------------------------------- moveShots (lib/actions.js)
def move_shots_ops(shots: list[dict], ids: list[str], index: int) -> tuple[list[dict], int]:
    """moveShots: the shot.move ops that put the listed shots, in their current order, at `index` among the
    others; and how many shots move as the block."""
    wanted = set(ids)
    block = [s for s in shots if s["id"] in wanted]
    rest = [s for s in shots if s["id"] not in wanted]
    at = max(0, min(len(rest), index))
    want = [s["id"] for s in [*rest[:at], *block, *rest[at:]]]
    order = [s["id"] for s in shots]
    ops = []
    for i, sid in enumerate(want):
        if order[i] == sid:
            continue
        order.remove(sid)
        order.insert(i, sid)
        ops.append({"op": "shot.move", "id": sid, "index": i})
    return ops, len(block)


def _move_shots(project: dict, data: dict) -> dict:
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise AppError("ids 要是鏡頭 id 的清單")
    index = data.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise AppError("index 要是整數（移到其餘鏡頭的第幾個位置）")
    ops, n = move_shots_ops(project["shots"], ids, index)
    if not n:
        raise AppError("找不到這些鏡頭")
    if not ops:
        raise AppError("鏡頭已經在這個位置")
    return {"ops": ops, "label": f"移動 {n} 個鏡頭"}


CMDS: dict[str, Callable[[dict, dict], dict]] = {
    "declip": _declip,
    "lift": _lift,
    "drop": _drop,
    "rippleDelete": _ripple_delete,
    "title": _title,
    "glossaryFix": _glossary_fix,
    "place": _place,
    "moveShots": _move_shots,
}
