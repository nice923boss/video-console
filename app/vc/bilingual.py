"""Bilingual captions from a Chinese/English parallel text (中英對照), such as a Word file whose paragraphs
alternate between a Chinese line and its English translation.

Each Chinese line is paired with the English under it; a pair too wide for the frame is cut into pieces at
punctuation where it can be, the Chinese and the English into the same number of pieces. The Chinese pieces
go on a caption track of their own and the English on another below it, as one line or as two, each English
caption pointing at its Chinese one (`pair`) so speech alignment times both together. Until then the pieces
follow each other at a reading pace.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from .captions import wrap_text, wrap_width
from .fontmetrics import measurer
from .opfields import DEFAULT_CAPTION_STYLE
from .util import AppError, new_id
from .voice import replace_ops

MAX_TEXT = 300_000
MAX_PAIRS = 3000
MAX_PIECES = 8  # most pieces one pair is cut into
ZH_TRACK = "中文"
EN_TRACK = "English"
# Both sit in the bottom row: the English (the lower track) nearest the frame edge, the Chinese right above it.
ZH_STYLE = {"name": "雙語中", "font": "Microsoft JhengHei", "size": 54, "bold": True, "outlineWidth": 3.0,
            "marginV": 50.0, "marginH": 60.0}
EN_STYLE = {"name": "English", "font": "Arial", "size": 36, "bold": False, "outlineWidth": 2.5,
            "marginV": 50.0, "marginH": 60.0}
ENGLISH_MODES = ("single", "double")

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")
_BULLET = re.compile(r"^(?:[*•·‧▪●○◆■□]|-|–)\s+")
_ZH_STOP = set("，。、；：！？」』）…】》,.;:!?)")
_EN_STOP = set(",;:.!?)\"”’")
_NO_LINE_START = set("，。、．,.!?！？;；:：」』）)》〉】]}…%％")
_NO_LINE_END = set("「『（(《〈【[{")
_OPEN = dict(zip("「『（(《〈【[{“", "」』）)》〉】]}”"))
_CLOSE = set(_OPEN.values())


def is_chinese(line: str) -> bool:
    """A line counts as Chinese when its CJK characters are at least a fifth of its letters."""
    cjk = len(_CJK.findall(line))
    return cjk > 0 and cjk * 5 >= cjk + len(_LATIN.findall(line))


def _clean(line: str) -> str:
    line = line.replace("　", " ").replace("\t", " ").strip()
    return re.sub(r"\s{2,}", " ", _BULLET.sub("", line)).strip()


def pair_lines(text: Any) -> tuple[list[dict], list[str]]:
    """[{"zh", "en"}] from the text, and notes on what did not pair up.

    The document's own order decides which language leads: a pair starts at each line of the language its
    first line is in. A second line of the other language joins the first (a translation run over two
    paragraphs); a leading line with nothing under it stays alone.
    """
    if not isinstance(text, str) or not text.strip():
        raise AppError("沒有可以匯入的文字")
    if len(text) > MAX_TEXT:
        raise AppError("文字太長了（上限 30 萬字），請分段匯入")
    lines = [c for c in (_clean(raw) for raw in text.replace("﻿", "").splitlines()) if c]
    if not lines:
        raise AppError("沒有可以匯入的文字")
    lead = "zh" if is_chinese(lines[0]) else "en"
    pairs: list[dict] = []
    for line in lines:
        key = "zh" if is_chinese(line) else "en"
        if key == lead:
            pairs.append({"zh": "", "en": "", key: line})
        elif pairs[-1][key]:
            pairs[-1][key] += (" " if key == "en" else "") + line
        else:
            pairs[-1][key] = line
    if len(pairs) > MAX_PAIRS:
        raise AppError(f"一次最多匯入 {MAX_PAIRS} 組字幕，請分段匯入")
    notes = []
    zh_only = sum(1 for p in pairs if p["zh"] and not p["en"])
    en_only = sum(1 for p in pairs if p["en"] and not p["zh"])
    if zh_only:
        notes.append(f"{zh_only} 句中文沒有對應的英文")
    if en_only:
        notes.append(f"{en_only} 句英文沒有對應的中文")
    return pairs, notes


# ---------------------------------------------------------------- cutting pairs to fit
def _breaks(text: str, zh: bool) -> list[tuple[int, float]]:
    """Places text may be cut (index of the first character after the cut) with a penalty: none after
    punctuation, some between words or Chinese characters, more inside brackets or quotes (a title such as
    《書名》 stays whole) and before a number (Chapter 3)."""
    out = []
    depth = 0
    for i in range(1, len(text)):
        before, after = text[i - 1], text[i]
        if before in _OPEN:
            depth += 1
        elif before in _CLOSE and depth:
            depth -= 1
        if zh:
            if after in _NO_LINE_START or before in _NO_LINE_END or (before.isascii() and before.isalnum() and after.isascii() and after.isalnum()):
                continue
            out.append((i, 0.0 if before in _ZH_STOP else 0.45 if depth else 0.18))
        elif after == " ":
            penalty = 0.0 if before in _EN_STOP else 0.4 if depth else 0.12
            out.append((i, penalty + (0.15 if text[i + 1:i + 2].isdigit() else 0.0)))
    return out


def _split(text: str, n: int, zh: bool, width: Callable[[str], float]) -> list[str]:
    """text in n pieces of about equal width, cut where _breaks prefers; fewer when it has too few places."""
    if n <= 1 or not text:
        return [text]
    total = width(text) or 1.0
    cuts: list[int] = []
    breaks = _breaks(text, zh)
    for k in range(1, n):
        target = total * k / n
        options = [(abs(width(text[:i]) - target) / total + penalty, i) for i, penalty in breaks
                   if i > (cuts[-1] if cuts else 0)]
        if not options:
            break
        cuts.append(min(options)[1])
    pieces = [text[a:b].strip() for a, b in zip([0, *cuts], [*cuts, len(text)])]
    return [p for p in pieces if p]


def _two_lines(text: str, width: Callable[[str], float], limit: float) -> str:
    """English too wide for one line, broken once where the two lines come out most even."""
    if width(text) <= limit:
        return text
    best = None
    for i, penalty in _breaks(text, False):
        left, right = text[:i].strip(), text[i:].strip()
        score = max(width(left), width(right)) * (1 + penalty / 4)
        if best is None or score < best[0]:
            best = (score, left + "\n" + right)
    return best[1] if best else text


def _fits(zh: str, en: str, english: str, zh_w: Callable, en_w: Callable, zh_limit: float, en_limit: float) -> bool:
    if zh and len(wrap_text(zh, zh_limit, zh_w)) > 1:
        return False
    return not en or len(wrap_text(en, en_limit, en_w)) <= (2 if english == "double" else 1)


def segment_pairs(pairs: list[dict], zh_style: dict, en_style: dict, play_x: int,
                  english: str = "double") -> list[dict]:
    """[{"zh", "en", "split"}]: each pair, or its pieces when it does not fit the frame (the Chinese on one
    line, the English on one or two)."""
    if english not in ENGLISH_MODES:
        raise AppError("英文字幕行數設定不正確")
    zh_w = measurer(zh_style["font"], zh_style["bold"], float(zh_style["size"]), float(zh_style["spacing"]))
    en_w = measurer(en_style["font"], en_style["bold"], float(en_style["size"]), float(en_style["spacing"]))
    zh_limit, en_limit = wrap_width(zh_style, None, play_x), wrap_width(en_style, None, play_x)
    out = []
    for pair in pairs:
        zh, en = pair["zh"], pair["en"]
        lines = 2 if english == "double" else 1
        n = max(1, math.ceil(zh_w(zh) / zh_limit), math.ceil(en_w(en) / (en_limit * lines * 0.92)))
        while True:
            zhs, ens = _split(zh, n, True, zh_w), _split(en, n, False, en_w)
            count = max(len(zhs), len(ens))
            zhs += [""] * (count - len(zhs))
            ens += [""] * (count - len(ens))
            if n >= MAX_PIECES or all(_fits(a, b, english, zh_w, en_w, zh_limit, en_limit) for a, b in zip(zhs, ens)):
                break
            n += 1
        for a, b in zip(zhs, ens):
            if english == "double":
                b = _two_lines(b, en_w, en_limit)
            out.append({"zh": a, "en": b, "split": count > 1})
    return out


def reading_time(seg: dict) -> float:
    """Seconds a piece stays up before speech alignment: about four Chinese characters (or 2.5 English words)
    a second, between 1.2 and 8 seconds."""
    if seg["zh"]:
        n = len(re.sub(r"[\s，。、；：！？「」『』（）…,.;:!?()\"]", "", seg["zh"]))
        secs = 0.5 + n * 0.25
    else:
        secs = 0.5 + len(seg["en"].split()) * 0.4
    return round(min(8.0, max(1.2, secs)), 3)


# ---------------------------------------------------------------- project ops
def _style(project: dict, base: dict) -> tuple[dict, list[dict]]:
    """The project style named like base (as it is now), else base as a new style and the op adding it."""
    for style in project.get("captionStyles") or []:
        if style.get("name") == base["name"]:
            return {**DEFAULT_CAPTION_STYLE, **style}, []
    style = {**DEFAULT_CAPTION_STYLE, **base, "id": new_id("cs")}
    return style, [{"op": "captionStyle.add", "item": style}]


def _track(project: dict, name: str, index: int | None) -> tuple[str, list[dict]]:
    for track in project.get("captionTracks") or []:
        if track.get("name") == name:
            return track["id"], []
    track_id = new_id("ct")
    return track_id, [{"op": "captionTrack.add", "track": {"id": track_id, "name": name}, "index": index}]


def bilingual_plan(project: dict, text: Any, english: str = "double", start: float = 0.0,
                   replace: bool = False) -> dict:
    """{"ops", "segments", "notes", "tracks"}: ops adding the text's captions on the 中文 and English caption
    tracks (made, with their styles, when the project has none of that name) from start seconds on.

    replace first removes the captions on those two tracks, with their voice-overs."""
    pairs, notes = pair_lines(text)
    width, height = int(project["settings"]["width"]), int(project["settings"]["height"])
    play_x = max(1, round(1080 * width / height))
    zh_style, zh_style_ops = _style(project, ZH_STYLE)
    en_style, en_style_ops = _style(project, EN_STYLE)
    segments = segment_pairs(pairs, zh_style, en_style, play_x, english)
    zh_track, zh_track_ops = _track(project, ZH_TRACK, None)
    en_track, en_track_ops = _track(project, EN_TRACK, None)
    ops = [*zh_style_ops, *en_style_ops, *zh_track_ops, *en_track_ops]
    if replace:
        old = [c for c in project["captions"] if c.get("track") in (zh_track, en_track)]
        for cap in old:
            ops += [op for op in replace_ops(project, cap["id"]) if op not in ops]
        ops += [{"op": "caption.delete", "id": c["id"]} for c in old]
    t = max(0.0, float(start))
    out_segments = []
    for seg in segments:
        end = round(t + reading_time(seg), 3)
        zh_id = new_id("c") if seg["zh"] else None
        if zh_id:
            ops.append({"op": "caption.add", "item": {
                "id": zh_id, "start": round(t, 3), "end": end, "text": seg["zh"][:500], "position": "bottom",
                "style": zh_style["id"], "track": zh_track, "shot": None}})
        if seg["en"]:
            ops.append({"op": "caption.add", "item": {
                "id": new_id("c"), "start": round(t, 3), "end": end, "text": seg["en"][:500], "position": "bottom",
                "style": en_style["id"], "track": en_track, "pair": zh_id, "shot": None}})
        out_segments.append({**seg, "start": round(t, 3), "end": end})
        t = end
    split = sum(1 for p in segments if p["split"])
    if split:
        notes.append(f"{split} 段超出畫面寬度，已切成較短的字幕")
    return {"ops": ops, "segments": out_segments, "notes": notes, "pairs": len(pairs),
            "tracks": {"zh": zh_track, "en": en_track}}
