"""Lyrics alignment, the "lyrics" job: KTV lyric captions for the song on the timeline, from its lyrics.

The sequence's sound, mixed as the export mixes it (align.mix_wav), and the lyric lines go to lyrics_worker.py,
which Speech-to-subtitle's Python runs on the GPU: stable-ts times the known text to the singing. Each line
becomes a caption whose `kt` wipe marks (vc/ktv.py) follow its sung characters, on two rows laid out as in the
owner's reference KTV video: the first line of each pair on 歌詞上 (starting at the left), the second on 歌詞下
(ending at the right).

Paging, measured from that video (frame by frame at 15 fps):
- a section (the song's start, or the first line after a pause longer than SECTION_GAP) shows its first two lines
  PRE seconds before the first is sung, the upper row first;
- a sung line keeps the sung colour until the line two after it takes its row: the upper row changes UPPER_LEAD
  before that line is sung, the lower row LOWER_SWAP before the upper line after it is sung;
- the last two lines of a section stay until CLEAR after its last character.

The aligner misplaces what it cannot hear. Around an instrumental break it put a line's last character at the
end of the break and squeezed the next lines into no time at all. So a character far from the rest of its line
comes back next to it, characters given no time borrow some, and a line squeezed into no time counts as not
heard: it is spread over the pause before the next heard line and switched off until someone checks it.
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import statistics
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ktv
from .align import mix_wav
from .bilingual import _style, _track
from .edits import audio_length
from .jobs import JobControl
from .media import Media
from .sequence import build_sequence, total_duration
from .store import ProjectStore
from .stt import LANE, SttClient
from .util import AppError, fmt_seconds, new_id
from .voice import replace_ops

WORKER = Path(__file__).with_name("lyrics_worker.py")
MODEL = "medium"  # large-v3 was not better on the spike song and takes twice the memory
MAX_TEXT = 20000
MAX_LINES = 300
MAX_LINE = 60  # characters; a KTV line this long already wraps to several rows

SECTION_GAP = 6.0  # seconds without singing that end a section (the screen clears)
PRE = 3.0  # a section's first page shows this long before its first line is sung
CLEAR = 2.0  # a section's last page stays this long after its last character
UPPER_LEAD = 1.2  # the next upper line shows this long before it is sung
LOWER_SWAP = 0.2  # the next lower line shows this long before the upper line between them is sung
MIN_HOLD = 0.3  # a sung line stays at least this long in the sung colour

MIN_CHAR = 0.05  # a character the aligner gave less time than this was squeezed
MIN_LINE_CHAR = 0.08  # a line given less than this per character was not heard
JUMP = 3.0  # a pause this long inside a line is suspect, twice as long it is wrong
TYPICAL = 0.3  # seconds a sung character takes when the song gives no better guess
PAUSE = 0.4  # between lines placed by guess

UPPER_TRACK, LOWER_TRACK = "歌詞上", "歌詞下"
# The KTV presets of web/js/lib/caption-presets.js (tests/test_lyrics.py keeps them the same).
UPPER_STYLE = {"name": "KTV 上行（置左）", "size": 148, "outlineWidth": 6, "shadow": False, "highlight": True,
               "highlightColor": "#0509a3", "highlightOutline": "#ffffff", "align": "left", "x": 0.077, "y": 0.776}
LOWER_STYLE = {"name": "KTV 下行（置右）", "size": 148, "outlineWidth": 6, "shadow": False, "highlight": True,
               "highlightColor": "#0509a3", "highlightOutline": "#ffffff", "align": "right", "x": 0.937, "y": 0.917}

_TAG = re.compile(r"^[\[【［].*[\]】］]$")  # [Verse 1], 【副歌】: song structure, not sung
NOTE = ("#", "＃")  # a line starting so is a note (web/assets/lyrics-template.txt explains the format in them)
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힯]")

Span = tuple[float, float, float]  # a character's start, end and the aligner's probability


def parse_lyrics(text: Any) -> list[str]:
    """The lines to sing: blank lines, section tags such as [Chorus] and # notes dropped, inner spaces kept single."""
    if not isinstance(text, str) or not text.strip():
        raise AppError("沒有歌詞：請貼上歌詞文字，一行一句")
    if len(text) > MAX_TEXT:
        raise AppError(f"歌詞太長（上限 {MAX_TEXT} 字）", 413)
    lines = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or _TAG.match(line) or line.startswith(NOTE):
            continue
        if len(line) > MAX_LINE:
            raise AppError(f"第 {len(lines) + 1} 句歌詞超過 {MAX_LINE} 字，請分成較短的行：{line[:20]}…")
        lines.append(line)
    if not lines:
        raise AppError("歌詞裡只有段落標記或 # 說明，沒有要唱的句子")
    if len(lines) > MAX_LINES:
        raise AppError(f"歌詞超過 {MAX_LINES} 句")
    return lines


def language_of(lines: list[str], given: Any = None) -> str:
    """The Whisper language code: the one given, else guessed from the script the lyrics are written in."""
    if isinstance(given, str) and re.fullmatch(r"[a-z]{2,3}", given.strip().lower()):
        return given.strip().lower()
    text = "".join(lines)
    if _KANA.search(text):
        return "ja"
    if _HANGUL.search(text):
        return "ko"
    return "zh" if _CJK.search(text) else "en"


def _key(ch: str) -> str:
    return unicodedata.normalize("NFKC", ch).lower()


def heard_chars(words: list[dict]) -> list[tuple[str, Span]]:
    """Each character of the aligned words with its own share of the word's time (spaces left out)."""
    out = []
    for w in words:
        try:
            s, e, p = float(w["s"]), float(w["e"]), float(w.get("p") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        cs = [c for c in str(w.get("w") or "") if not c.isspace()]
        step = max(0.0, e - s) / len(cs) if cs else 0.0
        out += [(_key(c), (s + k * step, s + (k + 1) * step, p)) for k, c in enumerate(cs)]
    return out


def char_spans(lines: list[str], words: list[dict]) -> list[list[Span | None]]:
    """For each line, each of its non-space characters' span, matched to the aligned words in order; None for a
    character the aligner left out."""
    heard = heard_chars(words)
    seq, owner = [], []
    out: list[list[Span | None]] = []
    for i, line in enumerate(lines):
        cs = [c for c in ktv.chars(line) if not c.isspace()]
        seq += [_key(c) for c in cs]
        owner += [(i, j) for j in range(len(cs))]
        out.append([None] * len(cs))
    matcher = difflib.SequenceMatcher(None, seq, [h[0] for h in heard], autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            i, j = owner[a + k]
            out[i][j] = heard[b + k][1]
    return out


def typical_char(spans: list[list[Span | None]]) -> float:
    durs = [e - s for line in spans for sp in line if sp for s, e, _ in [sp] if e - s >= MIN_CHAR]
    return min(0.6, max(0.15, statistics.median(durs))) if durs else TYPICAL


def _fill_missing(line: list[Span | None]) -> list[Span]:
    """A character the aligner left out sits, taking no time, where the one before it ended."""
    out: list[Span] = []
    for k, sp in enumerate(line):
        if sp is None:
            nxt = next((x for x in line[k + 1:] if x), None)
            at = out[-1][1] if out else (nxt[0] if nxt else 0.0)
            sp = (at, at, 0.0)
        out.append(sp)
    return out


def _weak(part: list[Span]) -> bool:
    return all(e - s < MIN_CHAR for s, e, _ in part) or statistics.mean(p for _, _, p in part) < 0.3


def _retime(part: list[Span], at: float, typical: float, forward: bool) -> list[Span]:
    """part placed back to back from at (forward) or up to at, each character keeping its own time when the
    aligner gave it a believable one."""
    out = []
    for s, e, p in (part if forward else reversed(part)):
        d = min(e - s, typical * 2) if e - s >= MIN_CHAR else typical
        out.append((at, at + d, p) if forward else (at - d, at, p))
        at = at + d if forward else at - d
    return out if forward else out[::-1]


def snap_jumps(line: list[Span], typical: float) -> tuple[list[Span], bool]:
    """A long pause inside a line splits it; the smaller side comes back next to the rest when it looks
    misplaced (squeezed or unsure), or always when the pause is very long."""
    changed = False
    for _ in range(len(line)):
        gap, k = max(((line[k][0] - line[k - 1][1], k) for k in range(1, len(line))), default=(0.0, 0))
        if gap <= JUMP:
            break
        head, tail = line[:k], line[k:]
        small = tail if len(tail) <= len(head) else head
        if gap <= 2 * JUMP and not _weak(small):
            break
        line = head + _retime(tail, head[-1][1], typical, True) if small is tail \
            else _retime(head, tail[0][0], typical, False) + tail
        changed = True
    return line, changed


def heard(line: list[Span]) -> bool:
    """Whether the aligner found the line: it takes some time and most of its characters were given some."""
    squeezed = sum(1 for s, e, _ in line if e - s < MIN_CHAR)
    return line[-1][1] - line[0][0] >= MIN_LINE_CHAR * len(line) and squeezed * 2 <= len(line)


def fill_squeezed(line: list[Span], typical: float, lo: float) -> tuple[list[Span], bool]:
    """Characters given no time get some: at the start of the line from the pause before it (not before lo),
    elsewhere a share of the character after them (at the end: of the one before them)."""
    out = list(line)
    changed = False
    k = 0
    while k < len(out):
        if out[k][1] - out[k][0] >= MIN_CHAR:
            k += 1
            continue
        m = k
        while m < len(out) and out[m][1] - out[m][0] < MIN_CHAR:
            m += 1
        run = m - k
        if k == 0 and m < len(out) and out[m][0] - max(lo, out[m][0] - run * typical) >= run * MIN_CHAR:
            b = out[m][0]
            a = max(lo, b - run * typical)
            first, last = 0, m - 1
        elif m < len(out):
            a, b = out[k][0], out[m][1]
            first, last = k, m
        elif k > 0:
            a, b = out[k - 1][0], max(out[-1][1], out[k - 1][1])
            first, last = k - 1, m - 1
        else:  # every character squeezed: the line was not heard and is placed by guess instead
            break
        step = (b - a) / (last - first + 1)
        for j in range(first, last + 1):
            out[j] = (a + (j - first) * step, a + (j - first + 1) * step, out[j][2])
        changed = True
        k = last + 1
    return out, changed


def guess_unheard(found: list[list[Span] | None], sizes: list[int], typical: float) -> list[list[Span]]:
    """Each run of lines not heard, placed by guess to end just before the next heard line (after the last one
    when none follows), each as long as its characters typically take, squeezed when the pause is shorter."""
    out = list(found)
    i = 0
    while i < len(out):
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < len(out) and out[j] is None:
            j += 1
        before = next((out[k][-1][1] for k in range(i - 1, -1, -1) if out[k]), None)
        after = out[j][0][0] if j < len(out) else None
        need = [max(1.0, sizes[k] * typical) for k in range(i, j)]
        total = sum(need) + PAUSE * len(need)
        hi = after if after is not None else (before or 0.0) + PAUSE + total
        lo = before if before is not None else max(0.0, hi - total)
        f = min(1.0, max(hi - lo, 0.0) / total)
        at = hi - total * f
        for k, d in zip(range(i, j), need):
            n = sizes[k]
            out[k] = [(at + d * f * c / n, at + d * f * (c + 1) / n, 0.0) for c in range(n)]
            at += (d + PAUSE) * f
        i = j
    return out  # type: ignore[return-value]


def sung_spans(lines: list[str], words: list[dict]) -> tuple[list[list[Span]], list[int], list[int]]:
    """Each line's character spans after the repairs above, with the indexes of the lines not heard (placed by
    guess) and of the heard ones whose characters were partly moved."""
    raw = char_spans(lines, words)
    typical = typical_char(raw)
    found: list[list[Span] | None] = []
    unheard, repaired = [], []
    lo = 0.0  # where the heard line before ends
    for i, line in enumerate(raw):
        spans = None
        if any(line):
            spans, jumped = snap_jumps(_fill_missing(line), typical)
            if heard(spans):
                spans, filled = fill_squeezed(spans, typical, lo)
                if jumped or filled or None in line:
                    repaired.append(i)
                lo = spans[-1][1]
            else:
                spans = None
        if spans is None:
            unheard.append(i)
        found.append(spans)
    if not any(found):
        raise AppError("聽不出歌詞唱在哪裡：請確認時間軸上的聲音是這首歌，歌詞也是這首歌的", 422)
    return guess_unheard(found, [len(line) for line in raw], typical), unheard, repaired


def layout(spans: list[list[Span]]) -> list[tuple[float, float, int]]:
    """(start, end, row) of each line's caption, row 0 upper and 1 lower, paged as the reference video is (see
    the module's docstring)."""
    a = [s[0][0] for s in spans]
    b = [s[-1][1] for s in spans]
    groups: list[list[int]] = []
    for i in range(len(spans)):
        if groups and a[i] - max(b[k] for k in groups[-1]) <= SECTION_GAP:
            groups[-1].append(i)
        else:
            groups.append([i])
    pages = [max(0.0, a[g[0]] - PRE) for g in groups]
    out: list[tuple[float, float, int]] = []
    for n, group in enumerate(groups):
        clear = max(b[k] for k in group) + CLEAR
        if n + 1 < len(pages):
            clear = min(clear, pages[n + 1])
        for pos, i in enumerate(group):
            row = pos % 2
            start = pages[n] if pos < 2 else out[i - 2][1]
            if pos + 2 >= len(group):
                end = clear
            else:
                cue = a[i + 2] - UPPER_LEAD if row == 0 else a[i + 1] - LOWER_SWAP
                end = min(max(b[i] + MIN_HOLD, cue), a[i + 2])
            out.append((round(start, 3), round(max(end, start + 0.1), 3), row))
    return out


def line_marks(text: str, spans: list[Span], start: float) -> list[float]:
    """The caption's kt: when each character starts being sung (a space: when the one before it ended), then
    when the last one ends, in seconds after the caption's start."""
    at: list[float] = []
    k, prev_end = 0, spans[0][0]
    for ch in ktv.chars(text):
        if ch.isspace():
            at.append(prev_end)
            continue
        at.append(spans[k][0])
        prev_end = spans[k][1]
        k += 1
    at.append(spans[-1][1])
    out: list[float] = []
    for t in at:
        out.append(max(round(max(0.0, t - start), 3), out[-1] if out else 0.0))
    return out


def lyrics_plan(project: dict, lines: list[str], words: list[dict]) -> dict:
    """{"ops", "unheard", "repaired", "replaced"}: ops putting the lines, timed to the aligned words, on the 歌詞上
    and 歌詞下 caption tracks with the KTV styles (each made when the project has none of that name), in place of
    the captions those tracks had; the indexes of the lines not heard (switched off) and of those repaired."""
    spans, unheard, repaired = sung_spans(lines, words)
    upper_style, upper_style_ops = _style(project, UPPER_STYLE)
    lower_style, lower_style_ops = _style(project, LOWER_STYLE)
    upper, upper_ops = _track(project, UPPER_TRACK, None)
    lower, lower_ops = _track(project, LOWER_TRACK, None)
    ops = [*upper_style_ops, *lower_style_ops, *upper_ops, *lower_ops]
    old = [c for c in project["captions"] if c.get("track") in (upper, lower)]
    for cap in old:
        ops += [op for op in replace_ops(project, cap["id"]) if op not in ops]
    ops += [{"op": "caption.delete", "id": c["id"]} for c in old]
    for i, ((start, end, row), line, sung) in enumerate(zip(layout(spans), lines, spans)):
        item = {"id": new_id("c"), "start": start, "end": end, "text": line, "position": "bottom",
                "style": (lower_style if row else upper_style)["id"], "track": lower if row else upper,
                "shot": None, "kt": line_marks(line, sung, start)}
        ops.append({"op": "caption.add", "item": {**item, "disabled": True} if i in unheard else item})
    return {"ops": ops, "unheard": unheard, "repaired": repaired, "replaced": len(old)}


def song_end(project: dict) -> float:
    """Where the last unmuted clip of the music and voice rows ends on the timeline."""
    return max((float(a.get("start") or 0.0) + audio_length(project, a) for a in project.get("audio") or []
                if not a.get("muted")), default=0.0)


def _numbers(indexes: list[int], limit: int = 8) -> str:
    shown = "、".join(str(i + 1) for i in indexes[:limit])
    return shown + (f" 等 {len(indexes)} 句" if len(indexes) > limit else " 句")


def result_message(count: int, unheard: list[int], repaired: list[int]) -> str:
    msg = f"已對齊 {count} 句歌詞，放在「{UPPER_TRACK}」「{LOWER_TRACK}」字幕軌"
    if unheard:
        msg += f"；第 {_numbers(unheard)}聽不出來，已排在空檔並先停用，請檢查"
    if repaired:
        msg += f"；第 {_numbers(repaired)}有字的時間經過修正，請聽一下"
    return msg


class LyricsJobs:
    """The "lyrics" job: payload {"text": the lyrics, one sung line per row, "language": a Whisper code, or None
    to guess it from the text}."""

    def __init__(self, store: ProjectStore, client: SttClient, media: Callable[[], Media]):
        self._store = store
        self._client = client
        self._media = media

    def prepare(self, project: dict, payload: dict) -> dict:
        lines = parse_lyrics(payload.get("text"))
        picture = total_duration(build_sequence(project))
        if picture <= 0:
            raise AppError("時間軸上沒有畫面：先放入影片或背景圖片，再把歌曲放上時間軸（聲音只輸出到畫面結束）")
        end = song_end(project)
        if end > picture + 0.5:
            raise AppError(f"畫面只到 {fmt_seconds(picture)}，歌曲到 {fmt_seconds(end)}：對齊和輸出都只到畫面結束。"
                           "請先把畫面拉長到歌曲結束（例如拉長背景圖），或把歌曲剪短")
        self._client.python(self._client.folder())  # fail now, not after the queue wait
        if not WORKER.is_file():
            raise AppError("找不到歌詞對齊程式 lyrics_worker.py，請重新安裝 Video Console", 500)
        return {"title": f"歌詞對齊（KTV）：{len(lines)} 句", "lane": LANE, "shotId": None,
                "payload": {"text": "\n".join(lines), "language": language_of(lines, payload.get("language"))}}

    def execute(self, ctl: JobControl) -> dict:
        lines = parse_lyrics(ctl.payload["text"])
        pdir = self._store.project_dir(ctl.pid)
        work = pdir / "media" / "cache" / f"job-{ctl.id}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            ctl.progress(0.01, "混合時間軸的聲音")
            wav = work / "mix.wav"
            mix_wav(self._media(), self._store.get(ctl.pid), pdir, wav,
                    lambda f: ctl.progress(0.01 + 0.09 * f, "混合時間軸的聲音"), ctl.cancelled)
            ctl.check()
            job_file = work / "job.json"
            job_file.write_text(json.dumps({"input_file": str(wav), "lines": lines, "model": MODEL,
                                            "language": ctl.payload.get("language") or language_of(lines)},
                                           ensure_ascii=False), encoding="utf-8")
            done = self._client.run(job_file, ctl, lambda f, m: ctl.progress(0.1 + 0.85 * min(max(f, 0.0), 1.0), m),
                                    script=WORKER, label="歌詞對齊")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        words = [w for w in done.get("words") or [] if isinstance(w, dict)]
        project = self._store.get(ctl.pid)  # again: the timeline may have been edited while the job ran
        plan = lyrics_plan(project, lines, words)
        self._store.apply(ctl.pid, plan["ops"], origin="job", undoable=True, label="歌詞對齊")
        return {"message": result_message(len(lines), plan["unheard"], plan["repaired"]), "lines": len(lines),
                "unheard": [i + 1 for i in plan["unheard"]], "repaired": [i + 1 for i in plan["repaired"]]}


