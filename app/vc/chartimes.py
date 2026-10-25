"""Per-character start times of a voice-over line (CP-13 karaoke captions), from the audio and the known text.

Neither VoAI nor Audio8Voice returns timing, so this estimates it after synthesis without a speech model
(docs/spike-CP-13.md): silence detection finds the pauses, punctuation says which pauses end a phrase, each
phrase's characters share its voiced time by syllable count, then each character moves to the quietest frame
within 50 ms (syllables meet in a dip of loudness). Pure-Python port of tools/spike-cp13/cp13_align.py, mode
"snap" with the floor and pause length the spike settled on."""
from __future__ import annotations

import array
import math
import re
import sys
import wave
from pathlib import Path
from typing import Callable

HOP = 0.01  # seconds per frame
FLOOR_DB = 30.0  # frames quieter than the loudest by more than this count as silence (breaths sit 30-45 dB down)
MIN_PAUSE = 0.1  # seconds of silence that count as a pause
SNAP = 5  # frames a character may move towards the nearest dip
NEAR = 30  # frames an unpaired phrase boundary may move towards the quietest moment
PUNCT = set("，。！？、；：,.!?;:…")

SKIP = 0.4  # cost (seconds) of a punctuation mark the voice did not pause at
UNUSED = 1.5  # cost per second of a pause left inside a phrase: long pauses usually sit at punctuation
REWARD = 1.0  # reward per second of pause length for a pause taken as a phrase boundary


def frame_db(path: Path) -> list[float]:
    """Loudness (dBFS) of 20 ms windows every 10 ms of a 16-bit PCM WAV, channels averaged."""
    with wave.open(str(path), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: 只支援 16 位元 WAV")
    return samples_db(raw, sr, ch)


def samples_db(raw: bytes, sr: int, ch: int = 1) -> list[float]:
    """frame_db of raw little-endian 16-bit samples (ffmpeg's s16le)."""
    x = array.array("h")
    x.frombytes(raw[:len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        x.byteswap()
    hop = int(sr * HOP)
    block = hop * ch
    # Sum of squares per 10 ms block; a frame is two neighbouring blocks.
    sums = []
    for i in range(0, len(x) - block + 1, block):
        if ch == 1:
            sums.append(sum(v * v for v in x[i:i + block]))
        else:
            part = x[i:i + block]
            sums.append(sum((sum(part[k:k + ch]) / ch) ** 2 for k in range(0, block, ch)))
    scale = 32768.0 * 32768.0 * 2 * hop
    return [10 * math.log10((sums[i] + sums[i + 1]) / scale + 1e-18) for i in range(len(sums) - 1)]


def number_syllables(n: int) -> int:
    """Characters in the Mandarin reading of an integer: 60 -> 六十 (2), 25 -> 二十五 (3), 105 -> 一百零五 (4)."""
    if n < 10:
        return 1
    if n < 20:
        return 1 + (n % 10 > 0)
    s, units, zero = str(n), 0, False
    for i, d in enumerate(s):
        place = len(s) - 1 - i
        if d == "0":
            zero = True
            continue
        units += (1 if zero and units else 0) + 1 + (place > 0)
        zero = False
    return units


def weights(text: str) -> list[float]:
    """Spoken syllables per character; punctuation and spaces take no time."""
    w = [0.0] * len(text)
    for m in re.finditer(r"\d+|[A-Za-z]+|(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])|.", text, re.S):
        tok, a = m.group(), m.start()
        if tok.isdigit():
            share = number_syllables(int(tok)) / len(tok)
        elif tok.isascii() and tok.isalpha():
            # Short or all-capital words are spelled out; others get one syllable per vowel group.
            spelled = len(tok) <= 3 or (tok.isupper() and len(tok) <= 5)
            groups = len(re.findall(r"[aeiouy]+", tok.lower().removesuffix("e"))) or 1
            share = 1.0 if spelled else groups / len(tok)
        elif tok == "." and m.end() - a == 1 and 0 < a < len(text) - 1 and text[a + 1].isalnum():
            share = 1.0  # "P5.js": the dot is read out
        elif tok in PUNCT or tok.isspace():
            share = 0.0
        else:
            share = 1.0
        for k in range(a, m.end()):
            w[k] = share
    return w


def _quietest(db: list[float], lo: int, hi: int) -> int:
    return min(range(lo, hi), key=db.__getitem__)


def _pauses(voiced: list[bool], s: int, e: int, min_frames: int) -> list[tuple[int, int]]:
    out, k = [], s
    while k < e:
        if not voiced[k]:
            j = k
            while j < e and not voiced[j]:
                j += 1
            if j - k >= min_frames:
                out.append((k, j))
            k = j
        else:
            k += 1
    return out


def _spread(w: list[float], idx: list[int], frames: list[int]) -> dict[int, float]:
    """Characters `idx` share the voiced `frames` by weight; returns each character's start frame."""
    total = sum(w[i] for i in idx) or 1.0
    out, acc = {}, 0.0
    for i in idx:
        pos = acc / total * len(frames)
        out[i] = frames[min(int(pos), len(frames) - 1)] + (pos - int(pos))
        acc += w[i]
    return out


def _match_pauses(guesses: list[int], gaps: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Pair phrase boundaries with pauses in order (edit-distance style); an unpaired boundary stays at its guess."""
    n, k = len(guesses), len(gaps)
    inf = float("inf")
    cost = [[inf] * (k + 1) for _ in range(n + 1)]
    step: dict[tuple[int, int], tuple[int, int]] = {}
    cost[0][0] = 0.0
    for j in range(n + 1):
        for g in range(k + 1):
            here = cost[j][g]
            if here == inf:
                continue
            moves = []
            if j < n and g < k:
                a, b = gaps[g]
                moves.append((j + 1, g + 1, abs((a + b) / 2 - guesses[j]) * HOP - REWARD * (b - a) * HOP))
            if j < n:
                moves.append((j + 1, g, SKIP))
            if g < k:
                moves.append((j, g + 1, UNUSED * (gaps[g][1] - gaps[g][0]) * HOP))
            for nj, ng, c in moves:
                if here + c < cost[nj][ng]:
                    cost[nj][ng] = here + c
                    step[(nj, ng)] = (j, g)
    out, j, g = [], n, k
    while (j, g) != (0, 0):
        pj, pg = step[(j, g)]
        if pj < j:
            out.append(gaps[pg] if pg < g else (guesses[pj], guesses[pj]))
        j, g = pj, pg
    return out[::-1]


def align(text: str, db: list[float]) -> list[float | None]:
    """Start time (seconds into the audio) of each character of `text`; None for punctuation and spaces.
    All None when the text has nothing to say or the audio is silent."""
    w = weights(text)
    phrases, cur = [], []
    for i, c in enumerate(text):
        if w[i] > 0:
            cur.append(i)
        elif c in PUNCT and cur:
            phrases.append(cur)
            cur = []
    if cur:
        phrases.append(cur)
    peak = max(db, default=-200.0)
    voiced = [v > peak - FLOOR_DB for v in db]
    on = [f for f, v in enumerate(voiced) if v]
    if not phrases or not on or peak < -90:
        return [None] * len(text)
    s, e = on[0], on[-1] + 1
    gaps = _pauses(voiced, s, e, int(MIN_PAUSE / HOP))
    in_gap = [False] * len(db)
    for a, b in gaps:
        for f in range(a, b):
            in_gap[f] = True
    talk = [f for f in range(s, e) if not in_gap[f]]
    # Where each phrase boundary would fall if every syllable took the same voiced time.
    total, acc, guesses = sum(w), 0.0, []
    for ph in phrases[:-1]:
        acc += sum(w[i] for i in ph)
        guesses.append(talk[min(int(acc / total * len(talk)), len(talk) - 1)])
    chosen = _match_pauses(guesses, gaps)
    for n, (a, b) in enumerate(chosen):
        if a == b:  # the voice did not stop at this mark: take the quietest moment near the guess
            lo = max(s + 1, a - NEAR, chosen[n - 1][1] + 1 if n else 0)
            hi = min(e - 1, a + NEAR + 1, chosen[n + 1][0] if n + 1 < len(chosen) else e)
            d = _quietest(db, lo, hi) if hi > lo else a
            chosen[n] = (d, d)
    starts = [s] + [b for _, b in chosen]
    ends = [a for a, _ in chosen] + [e]
    frame_of: dict[int, float] = {}
    for ph, a, b in zip(phrases, starts, ends):
        frames = [f for f in range(a, b) if not in_gap[f]] or list(range(a, max(b, a + 1)))
        frame_of.update(_spread(w, ph, frames))
    firsts = {ph[0] for ph in phrases}
    for i, f in frame_of.items():
        if i not in firsts:
            frame_of[i] = _quietest(db, max(0, int(f) - SNAP), min(len(db), int(f) + SNAP + 1))
    return [round(frame_of[i] * HOP, 3) if i in frame_of else None for i in range(len(text))]


PCM_RATE = 16000


def char_times(path: Path, text: str, pcm: Callable[[Path, int], bytes]) -> list[float | None]:
    """align() for a voice file of any format: `pcm(path, rate)` decodes it to mono 16-bit samples (Media.pcm16)."""
    return align(text, samples_db(pcm(path, PCM_RATE), PCM_RATE))
