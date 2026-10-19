"""Turn a pasted script or shot list into shots, without any AI service.

Understands spreadsheet / Markdown tables with a header row, numbered shot lists
("1.", "1、", "（1）", "鏡頭1：", "Shot 1:", "第1鏡", "一、", "00:00-00:05"), labelled fields inside
a shot ("畫面：", "運鏡：", "旁白：", "秒數：" ...), and plain paragraphs, lines or sentences.
Word files (.docx) are turned into the same text first: their shot tables as one tab-separated table.
"""
from __future__ import annotations

import csv
import io
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from typing import Any

from .notext import strip_text_directions
from .util import AppError, clamp

MAX_TEXT = 100_000
MAX_SHOTS = 200
MIN_DURATION, MAX_DURATION = 0.5, 60.0
MODES = ("auto", "paragraphs", "lines", "sentences", "single")

# Field aliases (lower case). Header cells match exactly first, then by substring in _HEADER_PRIORITY order.
_ALIASES: dict[str, tuple[str, ...]] = {
    "camera": ("運鏡", "鏡頭運動", "攝影機運動", "攝影機", "camera", "camera movement", "cameramovement"),
    "size": ("景別", "鏡頭大小", "角度", "鏡位", "構圖", "shot size", "shotsize", "shot type", "shottype",
             "framing", "angle"),
    "caption": ("旁白", "字幕", "台詞", "對白", "對話", "口白", "聲部", "narration", "dialogue", "voiceover",
                "voice over", "caption", "subtitle", "os", "vo"),
    "motion": ("動作", "動態", "表演", "motion", "action"),
    "duration": ("秒數", "時長", "長度", "時間", "秒", "duration", "length", "time", "seconds", "sec"),
    "notes": ("備註", "附註", "注意事項", "音效", "音樂", "配樂", "轉場", "notes", "note", "remark", "remarks",
              "sfx", "bgm", "sound", "music", "transition"),
    "place": ("場景", "地點", "location", "setting"),
    "prompt": ("畫面描述", "畫面", "描述", "內容", "影部", "提示詞", "prompt", "visual", "visuals", "description",
               "image"),
    "name": ("標題", "名稱", "鏡頭名稱", "title", "name"),
    "index": ("#", "no", "no.", "編號", "序號", "鏡號", "鏡次", "場次", "shot", "鏡頭", "cut", "scene"),
}
# Picture / sound column names of AV scripts, for header cells only (exact match): a free-text "聲音：" stays text.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {"prompt": ("影像", "video"), "caption": ("聲音", "audio")}
_HEADER_PRIORITY = ("camera", "size", "caption", "motion", "duration", "notes", "place", "prompt", "name", "index")
_LABEL_FIELDS = ("camera", "size", "caption", "motion", "duration", "notes", "place", "prompt", "name")
_ALIAS_FIELD = {a: f for f in _LABEL_FIELDS for a in _ALIASES[f]}

_NUM = r"(?:\d{1,3}|[一二三四五六七八九十百〇零]{1,4})"
# After a word such as 鏡頭, Chinese numerals need a delimiter so "鏡頭十分穩定" is not shot 10.
_NUM_STRICT = r"(?:\d{1,3}|[一二三四五六七八九十百〇零]{1,4}(?=[\s:：.、,，)）\-\u2013\u2014]|$))"
_TC = r"\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?"
_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"[【\[]\s*(?:鏡頭|分鏡|shot|cut|scene)?\s*" + _NUM + r"\s*[】\]]\s*[:：.、]?"
    r"|(?:鏡頭|分鏡|鏡號|shot|cut|scene)\s*[#＃]?\s*" + _NUM_STRICT + r"\s*[:：.、)）\-\u2013\u2014]?"
    r"|第\s*" + _NUM + r"\s*(?:個鏡頭|鏡頭|鏡)\s*[:：.、]?"
    r"|[(（]\s*" + _NUM + r"\s*[)）]"
    r"|[#＃]\s*" + _NUM + r"\s*[:：.、]?"
    r"|" + _NUM + r"\s*(?:[、:：)）]|\.(?!\d))"
    r")\s*",
    re.IGNORECASE,
)
# "00:00-00:05 畫面..." numbers a shot and gives its length.
_TC_PREFIX_RE = re.compile(
    r"^\s*[\[(（【]?\s*(" + _TC + r")\s*(?:[-~～到至\u2013\u2014]+\s*(" + _TC + r")\s*)?[\])）】]?\s*[:：.、|｜]?\s*"
)
_HEADING_RE = re.compile(
    r"^\s*(?:第\s*" + _NUM + r"\s*[場幕集]|場景\s*" + _NUM_STRICT + r"|INT\.|EXT\.)[\s:：.、|｜]*", re.IGNORECASE
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•·・]+\s+|#{1,6}\s+)")
_BOLD_RE = re.compile(r"\*\*|__")
_LABEL_RE = re.compile(
    r"(?:^|(?<=[\s，,。；;|｜、]))(" + "|".join(sorted(map(re.escape, _ALIAS_FIELD), key=len, reverse=True))
    + r")\s*[:：]",
    re.IGNORECASE | re.MULTILINE,
)
_FREE_DURATION_RE = re.compile(
    r"(?:^|(?<=[\s，,。；;（(【\[]))(?:約|大約)?\s*(\d{1,3}(?:\.\d+)?)\s*(秒鐘?|seconds?|secs?|s)"
    r"(?=$|[\s，,。；;.）)】\]])",
    re.IGNORECASE,
)
_UNIT_RE = re.compile(r"\s*(?:秒鐘?|seconds?|secs?|s)(?=$|[\s\-~～到至\u2013\u2014])", re.IGNORECASE)
_EMPTY_BRACKETS_RE = re.compile(r"[（(【\[]\s*[)）】\]]")
_BRACKET_RE = re.compile(r"[（(【\[]([^（()）【】\[\]]*)[)）】\]]")
# Speech verbs before a quote are dropped with it; audible actions (大喊) stay in the picture.
_SPEECH = r"(?:[他她它我你]們?)?(?:說|道|問|答|唸|念)(?:著|道)?"
_ACTION = r"(?:大|輕聲|低聲|小聲)?(?:喊|叫|嘆|低語|喃喃)(?:著|道)?"
_ACTION_RE = re.compile(_ACTION)
_QUOTE_RE = re.compile(
    r"(" + _SPEECH + r"|" + _ACTION + r"|[:：])\s*[:：]?\s*[「『“\"]([^」』”\"]{1,300})[」』”\"]"
)
# One spoken line in a sound column: 陳凱：(內心獨白)「……」 -> speaker, how it is said, the words.
_SPEECH_LINE_RE = re.compile(r"^(?:([^「」:：()（）]{1,20})[:：])?\s*([(（][^)）]*[)）])?\s*「(.+)」[。．]?$")
# A clause that describes a camera move (so it belongs to the video motion text).
_MOVE_RE = re.compile(
    r"(運鏡|推鏡|拉鏡|搖鏡|移鏡|跟拍|跟鏡|空拍|航拍|手持鏡頭|固定鏡頭|鏡頭固定|鏡頭不動|環繞拍攝|鏡頭從"
    r"|(?:緩慢|慢慢|緩緩|快速)?(?:推進|推近|拉遠)"
    r"|鏡頭(?:緩慢|慢慢|快速|逐漸|緩緩|輕輕|稍微)?地?(?:向|往)?(?:前|後|左|右|上|下)?"
    r"(?:平移|推|拉|搖|移|升|降|跟|環|轉|晃|掃|帶|前進|後退)"
    r"|\b(?:dolly|tracking|panning|handheld)\b|\bzoom(?:s|ing)?\s+(?:in|out)\b"
    r"|\bpan(?:s|ning)?\s+(?:left|right|up|down|across)\b|\btilt(?:s|ing)?\s+(?:up|down)\b"
    r"|\bpush(?:es|ing)?\s+in\b|\bpull(?:s|ing)?\s+(?:out|back)\b"
    r"|\bcamera\s+(?:orbits|circles|moves|pushes|pulls|rises|follows|tracks|pans|tilts)\b"
    r"|\b(?:crane|drone|aerial)\s+shot\b)",
    re.IGNORECASE,
)
_TIMECODE_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)")
_RANGE_RE = re.compile(r"([\d:.]+)\s*[-~～到至\u2013\u2014]\s*([\d:.]+)")


# ---------------------------------------------------------------- small helpers
def _seconds(token: str) -> float | None:
    m = _TIMECODE_RE.fullmatch(token)
    if m:
        hours, minutes, secs = m.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(secs)
    try:
        return float(token)
    except ValueError:
        return None


def parse_duration(value: Any) -> float | None:
    """'5', '5秒', '5 s', '3-5秒', '00:05-00:10' -> seconds (clamped); None when it is not a length."""
    text = str(value).strip().lower().replace("大約", "").replace("約", "")
    text = _UNIT_RE.sub("", text).strip()
    m = _RANGE_RE.fullmatch(text)
    if m:
        a, b = _seconds(m.group(1)), _seconds(m.group(2))
        if a is None or b is None:
            return None
        seconds: float | None = b - a if (":" in text or a == 0) else b
    else:
        seconds = _seconds(text)
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return None
    return round(clamp(seconds, MIN_DURATION, MAX_DURATION), 2)


def _tidy(text: str) -> str:
    text = _EMPTY_BRACKETS_RE.sub("", text)
    text = re.sub(r"([，,。；;])(?:\s*[，,。；;])+", r"\1", text)
    text = re.sub(r"([，。；：、])[ \t]+", r"\1", text)
    text = re.sub(r"[，,；;、]+[ \t]*(?=\n)", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip(" \t\n，,；;：:、。．")


def _join(parts: list[str], sep: str = "，") -> str:
    kept = [p for p in (_tidy(x) for x in parts) if p]
    return (", " if all(p.isascii() for p in kept) else sep).join(kept)


def _looks_like_title(line: str) -> bool:
    return (len(line) <= 16 and not re.search(r"[，,。；;：:!?！？]|\.(?:\s|$)", line)
            and not _MOVE_RE.search(line))


def _cut(pattern: str, text: str) -> list[str]:
    """re.split(pattern, text) that never cuts inside a bracket pair such as （遠景，緩慢推進）."""
    pairs = [m.span() for m in _BRACKET_RE.finditer(text)]
    pieces, start = [], 0
    for m in re.finditer(pattern, text):
        if not any(a < m.start() < b for a, b in pairs):
            pieces.append(text[start:m.start()])
            start = m.end()
    return pieces + [text[start:]]


def _is_tail(sentence: str) -> bool:
    """A sentence that only gives a length or a camera move belongs to the sentence before it."""
    rest = _FREE_DURATION_RE.sub("", sentence)
    clauses = [c.strip() for c in _cut(r"[，,。；;！!？?]|\.(?:\s|$)", rest) if c.strip()]
    return all(_MOVE_RE.search(c) for c in clauses)


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _cut(r"(?<=[。！？!?；;])|(?<=\.\s)", text) if p.strip()]
    merged: list[str] = []
    for part in parts:
        lead = _BRACKET_RE.match(part)
        if merged and lead:  # a note such as （遠景，推進） after the full stop belongs to the sentence before it
            merged[-1] += (" " if merged[-1][-1].isascii() else "") + lead.group()
            part = part[lead.end():].strip()
        if not part:
            continue
        if merged and (len(merged[-1]) < 8 or _is_tail(part)):
            merged[-1] += (" " if merged[-1][-1].isascii() else "") + part
        else:
            merged.append(part)
    return merged


def normalize_shot(data: Any) -> dict | None:
    """Clean one parsed (or AI-written) shot; None when it has nothing usable."""
    if not isinstance(data, dict):
        return None

    def text(key: str, limit: int) -> str:
        value = data.get(key)
        if value is None:
            return ""
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value if v is not None)
        return re.sub(r"[ \t]+", " ", str(value)).strip()[:limit]

    shot: dict[str, Any] = {
        "name": re.sub(r"\s+", " ", text("name", 60)),
        "prompt": text("prompt", 4000),
        "motion": text("motion", 2000),
        "camera": text("camera", 200),
        "caption": text("caption", 500).strip("「」『』“”\"").rstrip("。．."),
        "notes": text("notes", 2000),
        "duration": None,
    }
    raw = data.get("duration")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) and raw > 0:
        shot["duration"] = round(clamp(float(raw), MIN_DURATION, MAX_DURATION), 2)
    elif isinstance(raw, str):
        shot["duration"] = parse_duration(raw)
    if not (shot["prompt"] or shot["motion"] or shot["name"] or shot["notes"]):
        return None
    return shot


def _finish(fields: dict[str, list[str]]) -> dict | None:
    duration: float | None = None
    leftovers: list[str] = []
    for value in fields.get("duration", []):
        seconds = parse_duration(value)
        if seconds is None:
            leftovers.append(value)  # e.g. 時間：黃昏 is part of the picture, not a length
        elif duration is None:
            duration = seconds
    picture = fields.get("size", []) + fields.get("place", []) + leftovers + fields.get("prompt", [])
    # "5. 標題字卡浮現" is an editing note for the title card, not something to draw or animate.
    cards: list[str] = []

    def shown(parts: list[str]) -> str:
        text, dropped = strip_text_directions(_join(parts))
        cards.extend(dropped)
        return _tidy(text)

    return normalize_shot({
        "name": " ".join(fields.get("name", [])),
        "prompt": shown(picture + fields.get("extra", [])),
        "motion": shown(fields.get("motion", [])),
        "camera": shown(fields.get("camera", [])),
        "duration": duration,
        "caption": "\n".join(p for p in (_tidy(x) for x in fields.get("caption", [])) if p),
        "notes": "\n".join(fields.get("notes", []) + cards),
    })


# ---------------------------------------------------------------- free text
def _split_motion(body: str) -> tuple[str, str]:
    """The first camera move and what follows it in the same sentence describe motion; the rest, the picture.

    When the text opens with the move, only the run of move clauses is motion. A bracketed note such as
    （遠景，緩慢推進） gives its moves to the motion and keeps the rest (the shot size) in the picture.
    """
    moves: list[str] = []

    def unwrap(m: re.Match) -> str:
        clauses = re.split(r"(?<=[，,、。；;！!？?\n])|(?<=\.\s)", m.group(1))
        if not any(_MOVE_RE.search(c) for c in clauses):
            return m.group()
        moves.extend(c for c in clauses if _MOVE_RE.search(c))
        return m.group()[0] + _tidy("".join(c for c in clauses if not _MOVE_RE.search(c))) + m.group()[-1]

    body = _BRACKET_RE.sub(unwrap, body)
    parts = [p for p in _cut(r"(?<=[，,。；;！!？?\n])|(?<=\.\s)", body) if p]
    idx = next((i for i, p in enumerate(parts) if _MOVE_RE.search(p)), None)
    if idx is None:
        motion = _join(moves)
        return (_tidy(body) or motion), motion
    end = idx + 1
    if idx == 0:
        while end < len(parts) and _MOVE_RE.search(parts[end]):
            end += 1
    else:
        while end < len(parts) and not re.search(r"[。！!？?\n]\s*$|\.\s+$", parts[end - 1]):
            end += 1
    motion = "".join(parts[idx:end])
    picture = "".join(parts[:idx] + parts[end:])
    picture, motion = _tidy(picture), _join([motion, *moves])
    return (picture or motion), motion


def _free_text(free: str, fields: dict[str, list[str]]) -> None:
    lines = [ln.strip() for ln in free.split("\n") if ln.strip()]
    if len(lines) > 1 and not fields.get("name") and _looks_like_title(lines[0]):
        fields["name"].append(lines.pop(0))
    body = "\n".join(lines)
    if not fields.get("duration"):
        for m in _FREE_DURATION_RE.finditer(body):
            if m.group(2).lower() == "s" and re.fullmatch(r"[2-9]0", m.group(1)):
                continue  # "80s" is a decade and "20s" an age, not a length
            fields["duration"].append(m.group(1))
            body = body[: m.start()] + body[m.end():]
            break

    def take_quote(m: re.Match) -> str:
        fields["caption"].append(m.group(2))
        return m.group(1) if _ACTION_RE.fullmatch(m.group(1)) else ""

    body = _QUOTE_RE.sub(take_quote, body)
    if fields.get("motion"):
        fields["prompt"].append(body)
        return
    picture, motion = _split_motion(body)
    fields["prompt"].append(picture)
    if motion:
        fields["motion"].append(motion)


def _parse_block(text: str) -> dict | None:
    fields: dict[str, list[str]] = defaultdict(list)
    tc = _TC_PREFIX_RE.match(text)
    if tc:
        text = text[tc.end():]
        if tc.group(2):
            fields["duration"].append(f"{tc.group(1)}-{tc.group(2)}")
    elif _PREFIX_RE.match(text):
        text = _PREFIX_RE.sub("", text, count=1)
    else:
        text = _HEADING_RE.sub("", text, count=1)
    matches = list(_LABEL_RE.finditer(text))
    free = (text[: matches[0].start()] if matches else text).strip()
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[m.end(): end].strip().strip("，,；;|｜").strip()
        if value:
            fields[_ALIAS_FIELD[m.group(1).lower()]].append(value)
    if free:
        if fields.get("prompt"):
            # Text before the labels is the title when it is short, otherwise more description.
            if len(free) <= 20 and "\n" not in free and not fields.get("name"):
                fields["name"].append(free.strip("：: "))
            else:
                fields["prompt"].insert(0, free)
        else:
            _free_text(free, fields)
    return _finish(fields)


def _clean_line(line: str) -> str:
    return _BULLET_RE.sub("", _BOLD_RE.sub("", line))


def _is_heading(line: str) -> bool:
    return len(line.strip()) <= 30 and bool(_HEADING_RE.match(line))


def _rows_are_shots(rows: list[str]) -> bool:
    """Several lines are separate shots unless they read as one shot's labelled fields."""
    per_row = [{_ALIAS_FIELD[m.group(1).lower()] for m in _LABEL_RE.finditer(r)} for r in rows]
    repeated = any(n >= 2 for n in Counter(f for found in per_row for f in found).values())
    return repeated or sum(1 for r in rows if _LABEL_RE.match(r)) < 2


def _blocks(lines: list[str], mode: str) -> tuple[list[str], str, list[str]]:
    lines = [_clean_line(ln) for ln in lines]
    notes: list[str] = []
    if mode == "single":
        return ["\n".join(lines).strip()], "single", notes
    heads = {i for i, ln in enumerate(lines) if _is_heading(ln)}
    starts = [i for i, ln in enumerate(lines)
              if i not in heads and (_TC_PREFIX_RE.match(ln) or _PREFIX_RE.match(ln))]
    if mode == "auto" and not starts and len(heads) >= 2:
        starts, heads = sorted(heads), set()  # scene headings are the only structure: one shot each
    lines = ["" if i in heads else ln for i, ln in enumerate(lines)]
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if mode == "auto" and starts and (len(starts) >= 2 or starts[0] == first):
        skipped = sum(1 for ln in lines[: starts[0]] if ln.strip())
        if skipped:
            notes.append(f"開頭 {skipped} 行沒有編號，已當成標題略過")
        bounds = starts + [len(lines)]
        return ["\n".join(lines[a:b]).strip() for a, b in zip(bounds, bounds[1:])], "numbered", notes
    text = "\n".join(lines).strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if mode == "paragraphs" or (mode == "auto" and len(paragraphs) >= 2):
        return paragraphs, "paragraphs", notes
    rows = [ln.strip() for ln in lines if ln.strip()]
    if mode == "lines" or (mode == "auto" and len(rows) >= 2 and _rows_are_shots(rows)):
        return rows, "lines", notes
    if mode == "sentences" or (mode == "auto" and len(rows) == 1 and not _LABEL_RE.search(text)):
        sentences = _sentences(" ".join(rows))
        if len(sentences) >= 2 or mode == "sentences":
            return sentences, "sentences", notes
    return [text], "single", notes


# ---------------------------------------------------------------- tables
def _table_rows(text: str) -> list[list[str]] | None:
    filled = [ln.strip() for ln in text.split("\n") if ln.strip()]
    piped = [ln for ln in filled if ln.startswith("|")]
    if len(piped) >= 2 and len(piped) >= 0.6 * len(filled):
        rows = []
        for ln in piped:
            inner = ln[1:-1] if ln.endswith("|") and len(ln) > 1 else ln[1:]
            cells = [c.strip() for c in inner.split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            rows.append([re.sub(r"<br\s*/?>", "\n", c, flags=re.I) for c in cells])
        return rows or None
    if "\t" in text:
        # csv handles cells copied from Excel / Google Sheets that contain line breaks ("...").
        parsed = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter="\t")]
        parsed = [r for r in parsed if any(r)]
        wide = sum(1 for r in parsed if sum(1 for c in r if c) >= 2)
        if wide >= 2 and wide >= 0.6 * len(parsed):
            return parsed
    return None


def _header_field(cell: str) -> str | None:
    key = re.sub(r"[\s*_`]", "", cell).lower()
    if not key or len(key) > 20:
        return None
    for field in _HEADER_PRIORITY:
        if key in _ALIASES[field] or key in _HEADER_ALIASES.get(field, ()):
            return field
    for field in _HEADER_PRIORITY:
        if any(len(a) >= (4 if a.isascii() else 2) and a in key for a in _ALIASES[field]):
            return field
    return None


def _is_index(value: str) -> bool:
    return bool(re.fullmatch(r"[#＃]?\s*" + _NUM + r"[.、]?", value.strip()))


def _speech_cell(value: str, fields: dict[str, list[str]]) -> None:
    """A sound column: spoken lines are the caption (their speakers go to the notes), 音樂：/音效： lines the notes."""
    lines = [ln.strip() for ln in value.split("\n") if ln.strip()]
    sound = [ln for ln in lines if (m := _LABEL_RE.match(ln)) and _ALIAS_FIELD[m.group(1).lower()] == "notes"]
    spoken = [(ln, _SPEECH_LINE_RE.match(ln)) for ln in lines if ln not in sound]
    if not sound and not any(m for _, m in spoken):
        fields["caption"].append(value)  # plain narration: the cell is the caption as it is
        return
    speakers: list[str] = []
    for line, m in spoken:
        fields["caption"].append(m.group(3) if m else line)
        who = ((m.group(1) or "").strip() + (m.group(2) or "")) if m else ""
        if who and who not in speakers:
            speakers.append(who)
    fields["notes"].extend(sound)
    if speakers:
        fields["notes"].append("說話者：" + "／".join(speakers))


def _parse_table(rows: list[list[str]]) -> list[dict]:
    header = [_header_field(c) for c in rows[0]]
    if header.count("prompt") > 1:  # 內容 next to a picture column (影部) names the shot
        header = ["name" if f == "prompt" and re.sub(r"\s", "", c) == "內容" else f for f, c in zip(header, rows[0])]
    has_header = sum(1 for h in header if h) >= min(2, len(rows[0]))
    shots = []
    for row in rows[1:] if has_header else rows:
        cells = [_BOLD_RE.sub("", c).strip() for c in row]
        fields: dict[str, list[str]] = defaultdict(list)
        if has_header:
            for i, value in enumerate(cells):
                field = header[i] if i < len(header) else None
                if not value:
                    continue
                if field == "index":
                    if not _is_index(value):
                        fields["name"].append(value)
                elif field == "caption":
                    _speech_cell(value, fields)
                else:
                    fields[field or "extra"].append(value)
        else:
            rest = [c for c in cells if c]
            if rest and _is_index(rest[0]):
                rest = rest[1:]
            for value in [c for c in rest if len(c) <= 12 and parse_duration(c) is not None][:1]:
                fields["duration"].append(value)
                rest.remove(value)
            if rest:
                main = max(range(len(rest)), key=lambda i: len(rest[i]))
                if main > 0 and len(rest[0]) <= 16:
                    fields["name"].append(rest[0])
                fields["prompt"].append(rest[main])
                fields["motion"].extend(rest[main + 1:])
        shot = _finish(fields)
        if shot:
            shots.append(shot)
    return shots


# ---------------------------------------------------------------- Word files
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_DOCX_XML = 64 * 1024 * 1024


def _docx_paragraph(p: ET.Element) -> str:
    out = []
    for run in p.iter(_W + "r"):
        for el in run:
            if el.tag == _W + "t":
                out.append(el.text or "")
            elif el.tag in (_W + "br", _W + "cr"):
                out.append("\n")
            elif el.tag == _W + "tab":
                out.append(" ")
    return "".join(out)


def docx_to_text(data: bytes) -> dict[str, Any]:
    """{"text", "notes"} from a .docx: its shot tables (those sharing the first one's header row) as one
    tab-separated table, or its paragraphs when it has no shot table."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z, z.open("word/document.xml") as f:
            xml = f.read(MAX_DOCX_XML + 1)
        if len(xml) > MAX_DOCX_XML:
            raise AppError("這個 Word 檔的內容太大，請分成幾個檔案匯入")
        body = ET.fromstring(xml).find(_W + "body")
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise AppError("無法讀取這個 Word 檔，請確認是 .docx（舊版 .doc 請先另存為 .docx）") from None
    paragraphs: list[str] = []
    header: list[str] | None = None
    rows: list[list[str]] = []
    tables = skipped = 0
    for child in body if body is not None else []:
        if child.tag == _W + "p":
            paragraphs.append(_docx_paragraph(child).strip())
        elif child.tag == _W + "tbl":
            cells = [["\n".join(_docx_paragraph(p) for p in tc.findall(_W + "p")).strip()
                      for tc in tr.findall(_W + "tc")] for tr in child.findall(_W + "tr")]
            if cells and header is None and sum(1 for c in cells[0] if _header_field(c)) >= 2:
                header = cells[0]
            if cells and cells[0] == header:
                tables += 1
                rows.extend(r for r in cells[1:] if any(r))
            else:
                skipped += 1
    notes = [f"另有 {skipped} 個表格沒有匯入（沒有標題列或欄位不同）"] if skipped else []
    if header is None:
        text = "\n".join(paragraphs).strip()
    else:
        buf = io.StringIO()
        csv.writer(buf, delimiter="\t", lineterminator="\n").writerows([header, *rows])
        text = buf.getvalue()
        outside = sum(1 for p in paragraphs if p)
        notes.insert(0, f"讀入 {tables} 個分鏡表，共 {len(rows)} 列")
        if outside:
            notes.append(f"表格外的 {outside} 段文字沒有匯入（例如標題、說明、段落標題）")
    if not text.strip():
        raise AppError("這個 Word 檔沒有文字可以匯入")
    return {"text": text, "notes": notes}


# ---------------------------------------------------------------- entry point
def parse_script(text: Any, mode: str = "auto") -> dict[str, Any]:
    """Return {"shots": [...], "mode": how the text was read, "notes": [...]}."""
    if not isinstance(text, str) or not text.strip():
        raise AppError("請先貼上劇本或分鏡文字")
    if len(text) > MAX_TEXT:
        raise AppError("文字太長了（上限 10 萬字），請分段匯入")
    if mode not in MODES:
        raise AppError("拆分方式不正確")
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").replace("\u3000", " ")
    rows = _table_rows(text) if mode == "auto" else None
    notes: list[str] = []
    if rows:
        shots, used = _parse_table(rows), "table"
    else:
        blocks, used, notes = _blocks(text.split("\n"), mode)
        shots = [s for s in (_parse_block(b) for b in blocks if b.strip()) if s]
    if not shots:
        raise AppError("沒有找到可以變成鏡頭的內容")
    if len(shots) > MAX_SHOTS:
        notes.append(f"一次最多匯入 {MAX_SHOTS} 個鏡頭，其餘已略過")
        shots = shots[:MAX_SHOTS]
    return {"shots": shots, "mode": used, "notes": notes}
