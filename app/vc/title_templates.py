"""Title card templates (GR-10) on the server, mirror of web/js/lib/title-templates.js: each part of a template is
a caption with its own style pinned at an anchor (title: true). tests/test_twins.py checks the data matches."""
from __future__ import annotations

from typing import Any

from .jscompat import r3
from .opfields import DEFAULT_CAPTION_STYLE, MAX_CAPTION_STYLES
from .util import AppError, new_id

# Anchors are fractions of the frame (x, y is the line's middle); sizes are pixels on a 1080-high frame.
TEMPLATES: tuple[dict, ...] = (
    {"id": "opening", "name": "片頭標題", "hint": "置中的大標題，下面一行副標", "len": 4, "parts": [
        {"key": "main", "label": "主標", "text": "影片標題",
         "style": {"size": 110, "outline": False, "shadowOpacity": 0.6, "shadowDistance": 3, "shadowBlur": 4,
                   "x": 0.5, "y": 0.46}},
        {"key": "sub", "label": "副標", "text": "副標題或日期",
         "style": {"size": 46, "bold": False, "spacing": 6, "color": "#f2d27a", "outline": False,
                   "shadowOpacity": 0.6, "shadowDistance": 2, "shadowBlur": 3, "x": 0.5, "y": 0.585}},
    ]},
    {"id": "chapter", "name": "章節標題", "hint": "畫面中央半透明底的章節名稱", "len": 3, "parts": [
        {"key": "main", "label": "章節", "text": "第一章　開場",
         "style": {"size": 72, "outline": False, "shadow": False, "box": True, "boxColor": "#000000",
                   "boxOpacity": 0.6, "boxPadding": 28, "x": 0.5, "y": 0.5}},
    ]},
    {"id": "lower-box", "name": "下標字卡（色塊）", "hint": "左下角姓名與職稱，各有底色", "len": 5, "parts": [
        {"key": "name", "label": "姓名", "text": "王小明",
         "style": {"size": 58, "outline": False, "shadow": False, "box": True, "boxColor": "#0b3a75",
                   "boxOpacity": 0.92, "boxPadding": 14, "align": "left", "x": 0.06, "y": 0.78}},
        {"key": "role", "label": "職稱", "text": "產品經理",
         "style": {"size": 38, "bold": False, "color": "#0b3a75", "outline": False, "shadow": False, "box": True,
                   "boxColor": "#ffffff", "boxOpacity": 0.92, "boxPadding": 10, "align": "left", "x": 0.06,
                   "y": 0.865}},
    ]},
    {"id": "lower-plain", "name": "下標字卡（簡潔）", "hint": "左下角姓名與職稱，不加底色", "len": 5, "parts": [
        {"key": "name", "label": "姓名", "text": "王小明",
         "style": {"size": 60, "outlineWidth": 2, "shadowOpacity": 0.6, "shadowDistance": 2, "align": "left",
                   "x": 0.06, "y": 0.79}},
        {"key": "role", "label": "職稱", "text": "產品經理",
         "style": {"size": 40, "bold": False, "color": "#ffd400", "outlineWidth": 2, "shadowOpacity": 0.6,
                   "shadowDistance": 2, "align": "left", "x": 0.06, "y": 0.86}},
    ]},
    {"id": "corner", "name": "角落標籤", "hint": "左上角的地點、日期或節目名", "len": 5, "parts": [
        {"key": "main", "label": "標籤", "text": "台北．2026",
         "style": {"size": 40, "outline": False, "shadow": False, "box": True, "boxColor": "#c8102e",
                   "boxOpacity": 1, "boxPadding": 10, "align": "left", "x": 0.05, "y": 0.09}},
    ]},
)
LOOK_KEYS = tuple(k for k in DEFAULT_CAPTION_STYLE if k != "name")
LEN_RANGE = (1.0, 30.0)  # seconds the title dialog allows


def template(tid: Any) -> dict | None:
    return next((t for t in TEMPLATES if t["id"] == tid), None)


def look_of(tpl: dict, part: dict, v: dict | None = None) -> dict:
    """lookOf: a part's full caption style, with the colours the user picked (v: {color, boxColor})."""
    v = v or {}
    look = {**DEFAULT_CAPTION_STYLE, **part["style"], "name": f"{tpl['name']}：{part['label']}"}
    return {**look, "color": v.get("color") or look["color"], "boxColor": v.get("boxColor") or look["boxColor"]}


def _same(a: Any, b: Any) -> bool:
    """a === b for JSON values, where true and 1 differ."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _same_look(style: dict, look: dict) -> bool:
    return all(k in style and _same(style[k], look[k]) for k in LOOK_KEYS)


def unique_name(styles: list[dict], name: str) -> str:
    """uniqueName in lib/caption-style.js: name, or name with the first free number from 2."""
    taken = {s.get("name") for s in styles}
    if name not in taken:
        return name
    n = 2
    while f"{name[:36]} {n}" in taken:
        n += 1
    return f"{name[:36]} {n}"


def title_ops(project: dict, tpl: dict, values: dict, start: float, length: float | None = None) -> dict:
    """titleOps: {ops, first}, a style per part (one already in the project that looks the same is reused) and a
    caption per part with text, at start for length seconds."""
    length = tpl["len"] if length is None else length
    styles = list(project.get("captionStyles") or [])
    style_ops: list[dict] = []
    caps: list[dict] = []
    for part in tpl["parts"]:
        v = values.get(part["key"]) or {}
        text = str(v.get("text") or "").strip()
        if not text:
            continue
        look = look_of(tpl, part, v)
        style = next((s for s in styles if _same_look(s, look)), None)
        if not style:
            style = {**look, "id": new_id("cs"), "name": unique_name(styles, look["name"])}
            styles.append(style)
            style_ops.append({"op": "captionStyle.add", "item": style})
        caps.append({"op": "caption.add", "item": {"id": new_id("c"), "start": r3(start), "end": r3(start + length),
                                                   "text": text, "position": "middle", "style": style["id"],
                                                   "title": True}})
    if not caps:
        raise AppError("請輸入標題文字")
    if len(styles) > MAX_CAPTION_STYLES:
        raise AppError(f"字幕樣式最多 {MAX_CAPTION_STYLES} 個，請先在字幕樣式刪掉用不到的")
    return {"ops": style_ops + caps, "first": caps[0]["item"]["id"]}


def template_list() -> list[dict]:
    """GET /api/title-templates: each template with its fields, sample text and starting colours (boxColor None
    where the part has no box, as the dialog shows 底色 only then)."""
    out = []
    for tpl in TEMPLATES:
        fields = []
        for part in tpl["parts"]:
            look = look_of(tpl, part)
            fields.append({"key": part["key"], "label": part["label"], "text": part["text"], "color": look["color"],
                           "boxColor": look["boxColor"] if look["box"] else None})
        out.append({"id": tpl["id"], "name": tpl["name"], "hint": tpl["hint"], "len": tpl["len"], "fields": fields})
    return out
