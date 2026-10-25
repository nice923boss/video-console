"""CP-13 spike: estimate when each character of a voice-over line starts, from the audio and the known text.

No speech model: silence detection finds the pauses, punctuation says which pauses end a phrase, and each phrase's
characters share its voiced time by their syllable count; a mark the voice did not pause at goes to the quietest
moment near its guess. Variant "snap" then moves each estimate to the quietest frame within 50 ms (syllables meet in
a dip of loudness). Ground truth comes from the Windows voice's word events (sapi_gen.ps1).

Usage: python cp13_align.py <folder with sapi_r*_NN.wav/.tsv> [floor ...]   (the report uses floor 30)
"""
from __future__ import annotations

import re
import sys
import wave
from pathlib import Path

import numpy as np

HOP = 0.01  # seconds per frame
PUNCT = set("，。！？、；：,.!?;:…")
DIGITS = "零一二三四五六七八九"


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: {width * 8}-bit audio is not handled by this spike")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    return x.reshape(-1, ch).mean(axis=1), sr


def frame_db(x: np.ndarray, sr: int) -> np.ndarray:
    hop, win = int(sr * HOP), int(sr * HOP * 2)
    n = max(1, (len(x) - win) // hop + 1)
    rms = np.array([np.sqrt(np.mean(x[i * hop:i * hop + win] ** 2)) for i in range(n)])
    return 20 * np.log10(rms + 1e-9)


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
    for m in re.finditer(r"\d+|[A-Za-z]+|(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])|.", text):
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


def pauses_in(voiced: np.ndarray, s: int, e: int, min_frames: int) -> list[tuple[int, int]]:
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


def spread(w: list[float], idx: list[int], frames: list[int]) -> dict[int, float]:
    """Characters `idx` share the voiced `frames` by weight; returns each character's start frame."""
    total = sum(w[i] for i in idx) or 1.0
    out, acc = {}, 0.0
    for i in idx:
        pos = acc / total * len(frames)
        out[i] = frames[min(int(pos), len(frames) - 1)] + (pos - int(pos))
        acc += w[i]
    return out


SKIP = 0.4  # cost (seconds) of a punctuation mark the voice did not pause at
UNUSED = 1.5  # cost per second of a pause left inside a phrase: long pauses usually sit at punctuation
REWARD = 1.0  # reward per second of pause length for a pause taken as a phrase boundary


def match_pauses(guesses: list[int], gaps: list[tuple[int, int]]) -> list[tuple[int, int]]:
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


def align(text: str, db: np.ndarray, mode: str, min_pause: float = 0.1, floor: float = 40) -> dict[int, float]:
    """Start time (seconds) of each spoken character. mode: naive | phrase | snap."""
    voiced = db > db.max() - floor
    on = np.flatnonzero(voiced)
    s, e = int(on[0]), int(on[-1]) + 1
    w = weights(text) if mode != "naive" else [0.0 if c in PUNCT or c.isspace() else 1.0 for c in text]
    spoken = [i for i, x in enumerate(w) if x > 0]
    if mode == "naive":
        return {i: f * HOP for i, f in spread(w, spoken, list(range(s, e))).items()}
    phrases, cur = [], []
    for i, c in enumerate(text):
        if w[i] > 0:
            cur.append(i)
        elif c in PUNCT and cur:
            phrases.append(cur)
            cur = []
    if cur:
        phrases.append(cur)
    gaps = pauses_in(voiced, s, e, int(min_pause / HOP))
    in_gap = np.zeros(len(db), bool)
    for a, b in gaps:
        in_gap[a:b] = True
    talk = [f for f in range(s, e) if not in_gap[f]]
    # Where each phrase boundary would fall if every syllable took the same voiced time.
    total, acc, guesses = sum(w), 0.0, []
    for ph in phrases[:-1]:
        acc += sum(w[i] for i in ph)
        guesses.append(talk[min(int(acc / total * len(talk)), len(talk) - 1)])
    chosen = match_pauses(guesses, gaps)
    for n, (a, b) in enumerate(chosen):
        if a == b:  # the voice did not stop at this mark: take the quietest moment within 300 ms of the guess
            lo = max(s + 1, a - 30, chosen[n - 1][1] + 1 if n else 0)
            hi = min(e - 1, a + 31, chosen[n + 1][0] if n + 1 < len(chosen) else e)
            d = lo + int(np.argmin(db[lo:hi])) if hi > lo else a
            chosen[n] = (d, d)
    bounds = [s] + [b for _, b in chosen], [a for a, _ in chosen] + [e]
    out: dict[int, float] = {}
    for ph, a, b in zip(phrases, *bounds):
        frames = [f for f in range(a, b) if not in_gap[f]] or list(range(a, max(b, a + 1)))
        out.update(spread(w, ph, frames))
    if mode == "snap":
        firsts = {ph[0] for ph in phrases}
        for i, f in out.items():
            if i in firsts:
                continue
            lo, hi = max(0, int(f) - 5), min(len(db), int(f) + 6)
            out[i] = lo + int(np.argmin(db[lo:hi]))
    return {i: f * HOP for i, f in out.items()}


def main(folder: Path, floor: float) -> None:
    lines = [ln.strip() for ln in (Path(__file__).parent / "lines.txt").read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    for rate in sorted({p.name.split("_")[1] for p in folder.glob("sapi_r*.wav")}):
        errs: dict[str, list[float]] = {"naive": [], "phrase": [], "snap": []}
        onset_lag = []
        for n, text in enumerate(lines, 1):
            stem = folder / f"sapi_{rate}_{n:02d}"
            x, sr = read_wav(stem.with_suffix(".wav"))
            db = frame_db(x, sr)
            truth = [(int(a), float(c) / 1000) for a, _, c in
                     (r.split("\t") for r in stem.with_suffix(".tsv").read_text(encoding="utf-8").split("\n") if r)]
            voiced = db > db.max() - floor
            for pos, t in truth:  # how far the engine's mark sits before the sound actually starts
                f = int(t / HOP)
                nxt = np.flatnonzero(voiced[f:])
                onset_lag.append((nxt[0] if len(nxt) else 0) * HOP)
            for mode in errs:
                est = align(text, db, mode, floor=floor)
                errs[mode] += [est[pos] - t for pos, t in truth if pos in est]
        print(f"== {rate} floor {floor}: {len(errs['naive'])} word starts in {len(lines)} lines;"
              f" engine mark to sound onset median {np.median(onset_lag) * 1000:.0f} ms")
        for mode, e in errs.items():
            a = np.abs(np.array(e)) * 1000
            print(f"  {mode:6s} median {np.median(a):4.0f} ms  p90 {np.percentile(a, 90):4.0f} ms  max {a.max():4.0f} ms"
                  f"  <=100ms {np.mean(a <= 100):4.0%}  <=150ms {np.mean(a <= 150):4.0%}  bias {np.mean(e) * 1000:+.0f} ms")


if __name__ == "__main__":
    for fl in sys.argv[2:] or ["40"]:
        main(Path(sys.argv[1]), float(fl))
