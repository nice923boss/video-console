"""Plan the shots of a story beat the script already breaks down: numbered actions, spoken lines, camera notes.

A storyboard table often writes a row as a 【label】 and numbered actions (1. ... 2. (特寫) ...), with the
lines in a sound column and camera directions in the notes. Each action becomes a shot; a written close-up
stays a close-up; each spoken line is heard over the action it fits best (the speaker named in the action,
otherwise its place in the row), and a line that does not fit on that action gets a shot of its speaker.
A camera note (特寫陳凱的表情..., 鏡頭：用廣角...) becomes a shot of its own after the action it describes,
unless that action already shows it.

This reads the layout, not the story: it returns plain plans (size, picture, caption, role) that
vc.pacing times; a beat without that layout returns None and is covered by the rules in vc.pacing.
"""
from __future__ import annotations

import math
import re

FAST_LABEL_RE = re.compile(r"幻想|驚險|危險")  # a fantasy or a scare is cut fast
MAX_FANTASY = 3
MAX_LINE_CHARS = 26  # about six and a half seconds read aloud: a longer line is split across shots

_LABEL_RE = re.compile(r"^\s*【([^】]{1,20})】\s*")
_SCENE_RE = re.compile(r"^\s*場景\s*[:：]\s*")  # 場景：操場上，體育老師正在分組打籃球
_NUMBERED_RE = re.compile(r"^\s*\d{1,2}\s*[.、．)）]\s*")
_SIZES = r"大遠景|遠景|全景|中近景|中景|近景|大特寫|特寫|廣角"
_SIZE_RE = re.compile(_SIZES)
# a size that opens an action: (特寫) 一輛機車…, 特寫安全鎖已鎖定…, 鏡頭特寫桌子的尖角…
_LEAD_SIZE_RE = re.compile(r"^\s*(?:[(（]\s*(" + _SIZES + r")\s*[)）]|(?:鏡頭)?(" + _SIZES + r"))\s*[:：]?\s*")
_CAMERA_NOTE_RE = re.compile(r"^\s*(?:鏡頭\s*[:：]|(?:鏡頭)?用?(?:" + _SIZES + r"))")
_CAMERA_LEAD_RE = re.compile(r"^\s*鏡頭\s*[:：]?\s*用?")
_FACE_RE = re.compile(r"表情|神情|眼神|眼睛|臉|笑|哭|淚|嘟")
_NARRATOR_RE = re.compile(r"旁白|口白|字幕|^(?:os|vo)$", re.I)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]*")
_PHRASE_RE = re.compile(r"[^，,。！？!?；;～~…\n]+[，,。！？!?；;～~…\n]*")
_CHAR_RE = re.compile(r"[㐀-鿿豈-﫿]|[A-Za-z0-9]+")


def _chars(text: str) -> int:
    return len(_CHAR_RE.findall(text))


def _tidy(text: str) -> str:
    return text.strip(" \t\n，,。；;：:、")


def _actions(prompt: str) -> tuple[str, list[str], bool]:
    """(label, actions, numbered): numbered items, else the lines, else the sentences of the picture."""
    label = ""
    if m := _LABEL_RE.match(prompt):
        label, prompt = m.group(1).strip(), prompt[m.end():]
    lines = [ln.strip() for ln in prompt.split("\n") if ln.strip()]
    numbered = [ln for ln in lines if _NUMBERED_RE.match(ln)]
    if len(numbered) >= 2:
        # a line without a number continues the action above it
        items: list[str] = []
        for ln in lines:
            if _NUMBERED_RE.match(ln) or not items:
                items.append(_NUMBERED_RE.sub("", ln))
            else:
                items[-1] += ln
        return label, [a for a in (_tidy(i) for i in items) if a], True
    parts = lines if len(lines) > 1 else _SENTENCE_RE.findall(prompt)
    return label, [a for a in (_tidy(p) for p in parts) if a], False


def _sized(action: str) -> tuple[str | None, str]:
    """The size an action asks for and its picture without the size that opens it."""
    size = None
    for _ in range(2):  # (特寫) 鏡頭特寫桌子的尖角
        m = _LEAD_SIZE_RE.match(action)
        if not m or not action[m.end():].strip():
            break
        size, action = size or m.group(1) or m.group(2), action[m.end():]
    if not size and (inner := _SIZE_RE.search(action)):
        size = inner.group()
    return ("遠景" if size == "廣角" else size), _tidy(action)


def _camera_notes(notes: str) -> list[str]:
    """Notes lines that direct the camera to a shot: 特寫阮月安失落眼神…, 鏡頭：用廣角顯示…"""
    return [ln.strip() for ln in notes.split("\n") if _CAMERA_NOTE_RE.match(ln)]


def share(text: str, count: int) -> list[str]:
    """Text cut at its pauses into `count` parts of about the same length, in order ("" when too short)."""
    phrases = _PHRASE_RE.findall(text)
    if count <= 1 or len(phrases) <= 1:
        return [_tidy(text)] + [""] * max(count - 1, 0)
    ends, total = [], 0
    for p in phrases:
        total += max(_chars(p), 1)
        ends.append(total)
    cuts, start = [], 0
    for k in range(1, count):
        target = total * k / count
        best = min(range(start, len(phrases) + 1), key=lambda j: abs((ends[j - 1] if j else 0) - target))
        cuts.append(best)
        start = best
    bounds = [0] + cuts + [len(phrases)]
    return [_tidy("".join(phrases[a:b])) for a, b in zip(bounds, bounds[1:])]


def _listed(words: str) -> list[str] | None:
    """A narrator's list (不被理解的便當、被嘲笑的名字、因為身體的限制而被排擠。…): one part per item."""
    first = _SENTENCE_RE.match(words)
    items = first.group().split("、") if first else []
    if len(items) < 2 or any("，" in i or "," in i or not 4 <= _chars(i) <= 16 for i in items[:-1]):
        return None
    if not 2 <= _chars(re.split(r"[，,。！？!?]", items[-1])[0]) <= 16:
        return None
    rest = words[first.end():]
    return [_tidy(i) for i in items] + (share(rest, math.ceil(_chars(rest) / MAX_LINE_CHARS)) if rest.strip() else [])


def _units(lines: list[list[str]]) -> list[dict]:
    """Spoken parts in order: one speaker's lines next to each other joined while short, long ones split."""
    units: list[dict] = []
    for speaker, words, *how in lines:
        narrator = not speaker or bool(_NARRATOR_RE.search(speaker))
        last = units[-1] if units else None
        if last and last["speaker"] == speaker and _chars(last["words"] + words) <= MAX_LINE_CHARS:
            pause = "" if last["words"][-1] in "！？!?～~…" else "，"
            units[-1] = {**last, "words": last["words"] + pause + words}
            continue
        parts = (_listed(words) if narrator else None) or share(words, math.ceil(_chars(words) / MAX_LINE_CHARS))
        units.extend({"speaker": speaker, "how": (how or [""])[0], "words": p, "narrator": narrator}
                     for p in parts if p)
    return units


def _names(speaker: str) -> list[str]:
    return [n for n in re.split(r"[、／/和與]", speaker) if n]


def _mentions(unit: dict, action: str) -> bool:
    return not unit["narrator"] and any(n in action or (len(n) > 2 and n[-2:] in action)
                                        for n in _names(unit["speaker"]))


def _align(units: list[dict], actions: list[str]) -> list[int]:
    """The action each unit is heard over, in order: where its speaker is named, else its place in the row."""
    n, m = len(actions), len(units)
    if not n or not m:
        return []
    score = [[(2.0 if _mentions(u, a) else 0.0) - 0.5 * abs(j - ((i + 0.5) * n / m - 0.5))
              for j, a in enumerate(actions)] for i, u in enumerate(units)]
    best = [score[0][:]]
    back: list[list[int]] = [[0] * n]
    for i in range(1, m):
        row, from_ = [], []
        top, at = -math.inf, 0  # the best over the actions before j
        for j in range(n):
            stay = best[i - 1][j] - 1.0  # a second line on one action costs a shot of its own
            pick, src = (stay, j) if stay >= top else (top, at)
            row.append(score[i][j] + pick)
            from_.append(src)
            if best[i - 1][j] > top:
                top, at = best[i - 1][j], j
        best.append(row)
        back.append(from_)
    j = max(range(n), key=lambda k: best[-1][k])
    out = [j]
    for i in range(m - 1, 0, -1):
        j = back[i][j]
        out.append(j)
    return out[::-1]


def _bigrams(text: str) -> set[str]:
    cjk = "".join(re.findall(r"[㐀-鿿豈-﫿]", text))
    return {cjk[i:i + 2] for i in range(len(cjk) - 1)}


def _action_size(index: int, size: str | None, label: str, action: str, opening: bool) -> tuple[str, str]:
    """(size, role) of an action shot: a beat that opens a scene starts on a wide shot."""
    first = index == 0 and opening
    if size:
        return size, "close" if "特寫" in size else "open" if first else "action"
    if first:
        return ("遠景" if "空拍" in label else "全景"), "open"
    return ("近景" if _FACE_RE.search(action) else "中景"), "action"


def plan(shot: dict, opening: bool = False) -> dict | None:
    """{"label", "fast", "flash", "shots": [{"size", "picture", "caption", "role"}]} for a beat written as numbered
    actions, a storyboard table row or with camera notes, None for a beat the rules in vc.pacing cover instead.
    opening: the beat starts a scene (the caller knows the story's places); a row that opens with 場景： does."""
    label, actions, numbered = _actions(shot["prompt"])
    units = _units(shot.get("lines") or [])
    named = any(not u["narrator"] for u in units)
    notes = _camera_notes(shot["notes"])
    table = "lines" in shot
    # a storyboard table row is laid out as the writer cuts it; free text is left to the rules' several angles
    if not (numbered or actions and (notes or table) or units and not actions and (named or table)):
        return None
    if actions and (scene := _SCENE_RE.match(actions[0])):
        actions, opening = [_tidy(actions[0][scene.end():])] + actions[1:], True
    fast = bool(FAST_LABEL_RE.search(label))
    if "幻想" in label and len(actions) > MAX_FANTASY:  # a fantasy is a quick flash: its last actions share a shot
        actions = actions[:MAX_FANTASY - 1] + ["，".join(actions[MAX_FANTASY - 1:])]
    sized = [_sized(a) for a in actions]
    heard: list[list[dict]] = [[] for _ in actions]
    if "圖卡" in label and actions:  # an information card: the narration runs across its panels in order
        parts = share("，".join(u["words"] for u in units), len(actions))
        heard = [[{"words": p}] if p else [] for p in parts]
    else:
        for unit, j in zip(units, _align(units, [p for _, p in sized])):
            heard[j].append(unit)
    shots: list[dict] = []
    for i, ((written, picture), said) in enumerate(zip(sized, heard)):
        size, role = _action_size(i, written, label, picture, opening)
        shots.append({"size": size, "picture": picture, "caption": said[0]["words"] if said else "", "role": role})
        for unit in said[1:]:
            shots.append(_line_shot(unit, picture, shots[-1]["size"], written))
    if not actions:
        for unit in units:
            shots.append(_line_shot(unit, "", shots[-1]["size"] if shots else "", None))
    for note in notes:
        size, picture = _sized(_tidy(_CAMERA_LEAD_RE.sub("", note)))
        words = _bigrams(picture)
        overlap = [len(words & _bigrams(s["picture"])) if s["role"] != "camera" else 0 for s in shots]
        best = max(range(len(shots)), key=lambda k: (overlap[k], k)) if shots else -1
        hit = overlap[best] if shots else 0
        if hit and (hit * 2 >= len(words) or hit >= 2 and size == shots[best]["size"]):
            continue  # an action already shows what the note asks for
        after = best if hit >= 2 else len(shots) - 1
        while after + 1 < len(shots) and shots[after + 1]["role"] == "line":  # not between an action and its lines
            after += 1
        shots.insert(after + 1, {"size": size or "全景", "picture": picture, "caption": "", "role": "camera"})
    return {"label": label, "fast": fast, "flash": "幻想" in label, "shots": shots}


def _line_shot(unit: dict, picture: str, before: str, written: str | None) -> dict:
    """A line that does not fit on its action: its speaker, or for a narrator the action again, at the size the
    action asks for or else at another size than the shot `before` it."""
    if unit["narrator"]:
        size = written or next(s for s in ("全景", "中景", "近景") if s != before)
        return {"size": size, "picture": picture, "caption": unit["words"], "role": "line"}
    how = unit["how"].strip("()（） ")
    who = unit["speaker"] + "說話" + (f"，{how}" if how else "")
    return {"size": "特寫" if before == "近景" else "近景", "picture": f"{who}；{picture}" if picture else who,
            "caption": unit["words"], "role": "line"}
