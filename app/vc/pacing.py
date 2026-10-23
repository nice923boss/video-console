"""Break a script's story beats into several camera setups, timed by the story's pace and its narration.

A parsed shot without a length of its own is one story beat. Its pace (slow, medium, fast) comes from the
words in it and where it sits in the story; the pace and the narration's reading time set how long the beat
runs and how many shots cover it. Each shot gets its own size or angle (establishing, medium, close-up,
insert, reaction ...), a share of the beat's time weighted by that size, and the part of the narration
spoken while it is on screen. A shot that already gives its length is kept as it is.

This reads keywords, not the story: the result is a starting point to adjust in the review step.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .beats import plan
from .script import MAX_DURATION, MAX_SHOTS
from .util import clamp

PACES = ("slow", "medium", "fast")
PACE_NAMES = {"slow": "舒緩", "medium": "平穩", "fast": "緊湊"}
CHARS_PER_SECOND = 4.0  # Chinese narration read aloud
WORDS_PER_SECOND = 2.5
MAX_PER_BEAT = 5

# pace -> (shortest beat, average shot length, shortest shot, narration slack)
_PACE = {"slow": (7.0, 2.8, 2.0, 1.2), "medium": (4.5, 2.2, 1.5, 1.05), "fast": (3.0, 1.4, 0.8, 1.0)}
_FAST_RE = re.compile(
    r"突然|猛然|忽然|驟然|衝|追|逃|撞|爆|摔|搶|奔跑|跑向|打鬥|毆打|尖叫|大喊|怒吼|驚|慌|害怕|恐懼|緊張|焦急|著急|急"
    r"|危險|威脅|恐嚇|逼|命令|質問|警告|警報|犯罪|嫌犯|兇手|洗錢|緊皺|皺眉|心跳|冒汗|發抖|顫抖|快速|飛快|迅速|[！!]"
)
_SLOW_RE = re.compile(
    r"慢慢|緩緩|緩慢|靜靜|安靜|寧靜|平靜|冷靜|放鬆|深呼吸|柔和|溫暖|溫柔|安心|親切|微笑|相視|擁抱|陪伴|凝視|望著"
    r"|回憶|思念|夕陽|日落|晨光|清晨|黃昏|微風|悠閒|輕輕"
)
# A size or angle written in the text: its first shot keeps it.
_SIZE_RE = re.compile(r"(?:側面|正面|背面|俯拍|仰拍|低角度|高角度|插入)?(?:大遠景|遠景|全景|中近景|中景|近景|大特寫|特寫|過肩|廣角)")
# Text models sometimes write the picture in English: its size is read and written back in Chinese.
_EN_SIZE_RE = re.compile(r"\b(?:extreme\s+)?(?:(wide|establishing|medium\s+side|medium|insert|reaction)\s+shot|"
                         r"(close[- ]?up))\b", re.I)
_EN_SIZES = {"wide": "全景", "establishing": "全景", "medium side": "側面中景", "medium": "中景", "insert": "插入特寫",
             "reaction": "反應鏡頭"}
_TITLE_RE = re.compile(r"^(\S[^，,。；;：:\n]{0,11})[：:]\s*(?=\S)")
_PLACE_RE = re.compile(
    r"客廳|房間|臥室|廚房|餐桌|室內|街|巷|店|市場|公園|學校|教室|辦公室|醫院|車站|捷運|機場|局|署|中心|廣場|建築|外觀"
    r"|大樓|城市|鄉村|山|海|河|湖|港|天空|藍天|夜色|窗外|背景是|場景|校園|社區|門口|家中|家裡|屋"
)
_OUTDOOR_RE = re.compile(r"街|巷|公園|廣場|建築|外觀|大樓|城市|鄉村|山|海|河|湖|港|天空|藍天|白雲|戶外|森林|沙灘|田")
_PEOPLE_RE = re.compile(
    r"人|姨|伯|叔|舅|爺|奶|婆|媽|爸|父|母|兒|女|男|孩|童|學生|老師|醫生|員|警察(?![局署])|警官|長輩|老人|主角|他|她|們"
)
_FACE_RE = re.compile(r"表情|神情|眉|眼|笑|淚|哭|臉|嘴|凝視|望")
_DIALOGUE_RE = re.compile(r"說|問|答|喊|對話|通話|視訊|聽|兩人|一群|們")
# Objects worth an insert shot, most telling first.
_OBJECTS = ("手指", "按鍵", "按鈕", "手機", "螢幕", "電話", "鍵盤", "提款卡", "存摺", "鈔票", "信封", "紙條", "文件",
            "照片", "記事本", "眼鏡", "鑰匙", "戒指", "門鈴", "時鐘", "杯", "碗", "書", "花", "提款機", "電腦", "雙手")
_NARRATOR_RE = re.compile(r"^\s*(?:旁白|口白|字幕|OS|VO)\s*[:：]\s*", re.I)  # text models copy the script's label
_EMPTY_BRACKETS_RE = re.compile(r"[(（]\s*[)）]")  # what is left of (特寫) once its size is read
_CLAUSE_RE = re.compile(r"[，,。；;]")
_PHRASE_RE = re.compile(r"[^，,。！？!?；;、\n]+[，,。！？!?；;、\n]*")
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")

# kind -> (label, time weight, camera move by pace)
_KINDS: dict[str, tuple[str, float, dict[str, str]]] = {
    "wide": ("遠景", 1.3, {"slow": "緩慢推進", "medium": "緩慢推進", "fast": "快速推進"}),
    "medium": ("中景", 1.0, {"slow": "固定鏡頭", "medium": "固定鏡頭", "fast": "手持跟拍"}),
    "near": ("近景", 0.9, {"slow": "緩慢推近", "medium": "固定鏡頭", "fast": "手持"}),
    "side": ("側面中景", 0.9, {"slow": "緩慢橫移", "medium": "緩慢橫移", "fast": "快速橫移"}),
    "insert": ("插入特寫", 0.6, {"slow": "固定鏡頭", "medium": "固定鏡頭", "fast": "快速推近"}),
    "close": ("特寫", 0.8, {"slow": "緩慢推近", "medium": "固定鏡頭", "fast": "手持"}),
    "react": ("反應鏡頭", 0.7, {"slow": "固定鏡頭", "medium": "固定鏡頭", "fast": "手持"}),
}
_ORDER = ("wide", "medium", "near", "side", "insert", "close", "react")


def speech_seconds(text: str) -> float:
    """How long narration takes to read aloud."""
    return len(_CJK_RE.findall(text or "")) / CHARS_PER_SECOND + len(_WORD_RE.findall(text or "")) / WORDS_PER_SECOND


def _reading_time(caption: str) -> float:
    """Shortest shot that still shows its caption long enough to read, rounded up to a tenth of a second."""
    return math.ceil((speech_seconds(caption) + 0.2) * 10 - 1e-9) / 10 if caption else 0.0


def beat_pace(text: str, index: int, count: int) -> str:
    """slow / medium / fast from the beat's words; the opening and the ending lean slow."""
    score = len(_FAST_RE.findall(text)) - len(_SLOW_RE.findall(text))
    if index == 0 or index == count - 1:
        score -= 1
    return "fast" if score > 0 else "slow" if score < 0 else "medium"


def _size_kind(label: str, body: str) -> str:
    if re.search(r"遠景|全景|廣角", label):
        return "wide"
    if "特寫" in label:
        # a close-up of a hand or a phone is the beat's insert, not a face
        return "insert" if any(o in body for o in _OBJECTS) and not _FACE_RE.search(body) else "close"
    return "medium"


def _tidy(text: str) -> str:
    text = re.sub(r"([，,。；;：:])(?:\s*[，,。；;：:])+", r"\1", text)
    return text.strip(" \t\n，,。；;：:、")


def _read_beat(shot: dict) -> tuple[str, str, str | None]:
    """(title, picture without size words, the size the text asks for)."""
    prompt = shot["prompt"]
    title = shot["name"]
    m = _TITLE_RE.match(prompt)
    if m and not _SIZE_RE.fullmatch(m.group(1).strip()) and not _EN_SIZE_RE.fullmatch(m.group(1).strip()):
        title = title or m.group(1).strip()
        prompt = prompt[m.end():]
    size = _SIZE_RE.search(prompt)
    english = None if size else _EN_SIZE_RE.search(prompt)
    body = _tidy(_EMPTY_BRACKETS_RE.sub("", _EN_SIZE_RE.sub("", _SIZE_RE.sub("", prompt))))
    if english:
        word = re.sub(r"\s+", " ", (english.group(1) or "").lower())
        return title, body, _EN_SIZES.get(word, "特寫")
    return title, body, size.group() if size else None


def _kinds(body: str, caption: str, pace: str, first: bool, given: str | None, count: int) -> list[str]:
    """Which setups cover the beat, in the order they are cut together."""
    people = bool(_PEOPLE_RE.search(body))
    wanted = []
    if (first or _PLACE_RE.search(body)) and pace != "fast":
        wanted.append("wide")
    wanted.append("medium")
    if any(o in body for o in _OBJECTS):
        wanted.append("insert")
    if people:
        wanted.append("close")
    if people and (caption and _DIALOGUE_RE.search(body + caption)):
        wanted.append("react")
    lead = _size_kind(given, body) if given else None
    if _FACE_RE.search(body) and "close" in wanted:
        wanted.insert(1, wanted.pop(wanted.index("close")))  # a face that shows feeling earns its close-up first
    picked = [k for k in wanted + ["near", "side"] if k != lead][: count - (1 if lead else 0)]
    rest = sorted(picked, key=_ORDER.index)
    return ([lead] if lead else []) + rest


def _focus(kind: str, body: str) -> str:
    clauses = [c for c in (_tidy(x) for x in _CLAUSE_RE.split(body)) if c]
    if kind == "insert":
        for obj in _OBJECTS:
            hit = next((c for c in clauses if obj in c), None)
            if hit:
                return _tidy("，".join([hit] + [c for c in clauses if c != hit]))
    if kind == "close":
        return "臉部表情，" + body
    if kind == "react":
        return "聆聽者的神情反應，" + body
    return body


def _split_caption(caption: str, shares: list[float]) -> list[str]:
    """Cut narration at its pauses so each shot shows about the part read while it is on screen."""
    phrases = _PHRASE_RE.findall(caption) if caption else []
    if not phrases:
        return [""] * len(shares)
    ends, total = [], 0.0
    for p in phrases:
        total += max(speech_seconds(p), 0.1)
        ends.append(total)
    cuts, start, acc = [], 0, 0.0
    for share in shares[:-1]:
        acc += share
        target = acc * total
        best = min(range(start, len(phrases) + 1), key=lambda j: abs((ends[j - 1] if j else 0.0) - target))
        cuts.append(best)
        start = best
    bounds = [0] + cuts + [len(phrases)]
    return [_tidy("".join(phrases[a:b])) for a, b in zip(bounds, bounds[1:])]


def _expand(shot: dict, beat: int, pace: str, first: bool) -> list[dict]:
    title, body, given = _read_beat(shot)
    caption = shot["caption"]
    shortest, average, min_shot, slack = _PACE[pace]
    total = max(shortest, (speech_seconds(caption) + 0.5) * slack if caption else 0.0)
    count = int(clamp(math.ceil(total / average - 0.05), 2, MAX_PER_BEAT))
    kinds = _kinds(body or shot["prompt"], caption, pace, first, given, count)
    weights = [_KINDS[k][1] for k in kinds]
    shares = [w / sum(weights) for w in weights]
    parts = _split_caption(caption, shares)
    outdoor = bool(_OUTDOOR_RE.search(body))
    written_move = _written_move(shot)
    out = []
    for i, (kind, share, part) in enumerate(zip(kinds, shares, parts)):
        label, _, moves = _KINDS[kind]
        lead = i == 0 and bool(given)  # the size the writer asked for keeps the writer's own picture
        if lead:
            label = given
        elif kind == "wide":
            label = "遠景" if outdoor else "全景"
        move = written_move if i == 0 and written_move else moves[pace]
        need = _reading_time(part)
        seconds = max(round(clamp(max(total * share, min_shot), min_shot, MAX_DURATION), 1), min(need, MAX_DURATION))
        picture = body or shot["prompt"]
        out.append({
            "name": f"{title or f'段落 {beat}'}（{label}）"[:60],
            "prompt": f"{label}：{picture if lead else _focus(kind, picture)}",
            "motion": shot["motion"] if i == 0 else "",
            "camera": f"{label}，{move}",
            "caption": part,
            "notes": shot["notes"] if i == 0 else "",
            "duration": seconds,
            "beat": beat,
            "pace": pace,
        })
    return out


def _written_move(shot: dict) -> str:
    return _tidy(_EMPTY_BRACKETS_RE.sub("", _SIZE_RE.sub("", shot["camera"])))


def _planned(shot: dict, planned: dict, beat: int, pace: str) -> list[dict]:
    """A beat the script already breaks down (vc.beats): each planned shot as long as its role and its line ask.
    An establishing shot holds twice the beat's average, a close-up or a line shot and every shot of a fantasy
    flash the shortest, an action the average; a line is always given time to be read."""
    _, average, min_shot, _ = _PACE[pace]
    title = shot["name"] or planned["label"] or f"段落 {beat}"
    written_move = _written_move(shot)
    out = []
    for i, p in enumerate(planned["shots"]):
        size, role = p["size"], p["role"]
        kind = "near" if size == "近景" else _size_kind(size, p["picture"])
        move = written_move if i == 0 and written_move else _KINDS[kind][2][pace]
        if planned["flash"] or role in ("close", "line") or size == "特寫":
            least = min_shot
        elif role == "open":
            least = average * 2
        else:
            least = average
        seconds = round(min(max(least, _reading_time(p["caption"])), MAX_DURATION), 1)
        label = f"【{planned['label']}】\n" if planned["label"] else ""
        out.append({
            "name": f"{title}（{size}）"[:60],
            "prompt": f"{size}：{p['picture']}" if p["picture"] else "",  # the script shows nothing: left to fill in
            "motion": shot["motion"] if i == 0 else "",
            "camera": f"{size}，{move}",
            "caption": p["caption"],
            "notes": (label + shot["notes"]).strip() if i == 0 else "",
            "duration": seconds,
            "beat": beat,
            "pace": pace,
        })
    return out


def _new_scene(before: dict, shot: dict) -> bool:
    """Whether a beat moves to another place: a storyboard's scene column (教室角落 視線交會) names the place
    first, so 教室 then 教室角落 is one scene; without one, a place in the picture."""
    here, there = (s["name"].split()[0] if s["name"].split() else "" for s in (shot, before))
    if here and there:
        return not (here.startswith(there) or there.startswith(here))
    return bool(_PLACE_RE.search(shot["prompt"]))


def break_down(result: dict[str, Any]) -> dict[str, Any]:
    """parse_script's result with each untimed beat split into several shots: the shots the script already lays
    out (numbered actions, spoken lines, camera notes) when it does, otherwise shots of different sizes."""
    beats = result["shots"]
    shots: list[dict] = []
    kept = 0
    for i, shot in enumerate(beats):
        pace = beat_pace(" ".join((shot["prompt"], shot["motion"], shot["caption"])), i, len(beats))
        if shot["duration"]:
            kept += 1
            shots.append({**{k: v for k, v in shot.items() if k != "lines"}, "beat": i + 1, "pace": pace})
        elif planned := plan(shot, i == 0 or _new_scene(beats[i - 1], shot)):
            shots.extend(_planned(shot, planned, i + 1, "fast" if planned["fast"] else pace))
        else:
            shots.extend(_expand(shot, i + 1, pace, i == 0))
    notes = list(result["notes"])
    if kept:
        notes.append(f"{kept} 段已寫明秒數，維持一個鏡頭")
    if len(shots) > MAX_SHOTS:
        notes.append(f"細拆後超過 {MAX_SHOTS} 個鏡頭，只保留前 {MAX_SHOTS} 個")
        shots = shots[:MAX_SHOTS]
    return {**result, "shots": shots, "notes": pace_notes(shots) + notes}


def fill_coverage(shots: list[dict]) -> tuple[list[dict], list[str]]:
    """Text model shots with each beat the model left as one shot split by the rules, at the model's pace."""
    counts = Counter(s["beat"] for s in shots if s.get("beat"))
    order = sorted(counts)
    out: list[dict] = []
    filled = 0
    for shot in shots:
        beat = shot.get("beat")
        if not beat or counts[beat] > 1:
            out.append(shot)
            continue
        index = order.index(beat)
        pace = shot.get("pace") or beat_pace(" ".join((shot["prompt"], shot["motion"], shot["caption"])),
                                             index, len(order))
        out.extend(_expand(shot, beat, pace, index == 0))
        filled += 1
    notes = [f"文字模型有 {filled} 段只給一個鏡頭，已依規則補拆"] if filled else []
    if len(out) > MAX_SHOTS:
        notes.append(f"細拆後超過 {MAX_SHOTS} 個鏡頭，只保留前 {MAX_SHOTS} 個")
        out = out[:MAX_SHOTS]
    return out, notes


def beat_fields(item: Any) -> dict:
    """The beat number and pace a text model wrote for a shot, when they are usable."""
    out: dict[str, Any] = {}
    if not isinstance(item, dict):
        return out
    beat = item.get("beat")
    if isinstance(beat, (int, float)) and not isinstance(beat, bool) and 1 <= beat <= MAX_SHOTS:
        out["beat"] = int(beat)
    pace = str(item.get("pace") or "").strip().lower()
    if pace in PACES:
        out["pace"] = pace
    return out


def time_beats(shots: list[dict]) -> list[dict]:
    """Text model shots timed like the rules: each beat runs as long as its pace and narration ask, shared out by
    the model's own lengths. A caption the model repeated across a beat's shots is cut at its pauses instead."""
    groups: dict[int, list[int]] = {}
    for i, shot in enumerate(shots):
        if shot.get("beat"):
            groups.setdefault(shot["beat"], []).append(i)
    out = [{**s, "caption": _NARRATOR_RE.sub("", s["caption"])} for s in shots]
    order = sorted(groups)
    for n, beat in enumerate(order):
        idx = groups[beat]
        weights = [out[i]["duration"] or 1.0 for i in idx]
        captions = _unrepeat([out[i]["caption"] for i in idx], weights)
        pace = next((out[i]["pace"] for i in idx if out[i].get("pace")), None) or beat_pace(
            " ".join(" ".join((out[i]["prompt"], out[i]["motion"], out[i]["caption"])) for i in idx), n, len(order))
        shortest, _, min_shot, slack = _PACE[pace]
        narration = "".join(captions)
        total = max(shortest, (speech_seconds(narration) + 0.5) * slack if narration else 0.0)
        for i, caption, weight in zip(idx, captions, weights):
            seconds = round(clamp(total * weight / sum(weights), min_shot, MAX_DURATION), 1)
            out[i] = {**out[i], "caption": caption, "pace": pace,
                      "duration": max(seconds, min(_reading_time(caption), MAX_DURATION))}
    return fit_narration(out)


def _unrepeat(captions: list[str], weights: list[float]) -> list[str]:
    """Runs of one caption repeated on consecutive shots, cut so each shot shows its own part."""
    out: list[str] = []
    i = 0
    while i < len(captions):
        j = i + 1
        while j < len(captions) and captions[i] and captions[j] == captions[i]:
            j += 1
        run = weights[i:j]
        out.extend(_split_caption(captions[i], [w / sum(run) for w in run]) if j - i > 1 else captions[i:j])
        i = j
    return out


def fit_narration(shots: list[dict]) -> list[dict]:
    """Lengthen shots too short to read their own caption (text model output)."""
    out = []
    for shot in shots:
        need = _reading_time(shot["caption"])
        if need and (shot["duration"] or 0) < need:
            shot = {**shot, "duration": min(need, MAX_DURATION)}
        out.append(shot)
    return out


def pace_notes(shots: list[dict]) -> list[str]:
    """One line on how the beats were paced, for the review step."""
    beats = {s["beat"]: s.get("pace") for s in shots if s.get("beat")}
    if not beats:
        return []
    counts = Counter(p for p in beats.values() if p)
    paces = "、".join(f"{PACE_NAMES[p]} {counts[p]} 段" for p in PACES if counts[p])
    return [f"依劇情節奏分成 {len(beats)} 段、{len(shots)} 個鏡頭" + (f"：{paces}" if paces else "")]
