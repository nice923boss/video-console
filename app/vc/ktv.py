"""KTV lyric captions: the sung part of a line turns the style's highlight colour (and outline) as a wipe that
runs across each character while it is sung, as on a karaoke screen.

A caption's `kt` holds the wipe marks: seconds after its start at which each character of its trimmed text starts
turning, then the moment the last one is done, so one more mark than characters (opfields._kt). The lyrics
alignment (lyrics.py) writes them and the timeline drags them. A caption is wiped when its style has highlight on
and its marks fit its text; web/js/lib/karaoke.js draws the same wipe in the preview.

Edits keep the wipe on the words: a caption.update that changes the text carries each kept character's marks over
(new characters share the time of what they replaced), and one that moves only the start (a trim) keeps the marks
where they are on the timeline. Moving the whole caption takes its wipe along.
"""
from __future__ import annotations

import difflib
from collections.abc import Callable

MIN_STEP = 0.01  # seconds a character takes at least in the burnt-in wipe (libass needs t2 > t1)


def chars(text: str | None) -> list[str]:
    """The characters the marks count: the trimmed text by code point, as Python and the preview count them."""
    return list((text or "").strip())


def marks(cap: dict) -> list[float] | None:
    """The caption's wipe marks when they fit its text, else None (drawn without a wipe)."""
    kt = cap.get("kt")
    n = len(chars(cap.get("text")))
    return [float(t) for t in kt] if isinstance(kt, list) and n and len(kt) == n + 1 else None


def remap(old_text: str, new_text: str, kt: list[float]) -> list[float] | None:
    """Marks for new_text from the marks of old_text: kept characters keep theirs, a replaced run shares the
    time of what it replaced, inserted characters take no time and a removed one's time goes to the one before."""
    old, new = chars(old_text), chars(new_text)
    if len(kt) != len(old) + 1 or not new:
        return None
    out: list[float | None] = [None] * (len(new) + 1)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1 + 1):
                out[j1 + k] = kt[i1 + k]
        elif tag in ("replace", "insert"):
            a, b = kt[i1], kt[i2]
            for k in range(j2 - j1 + 1):
                out[j1 + k] = round(a + (b - a) * k / (j2 - j1), 3)
        # delete: j1 == j2, and the block after it sets out[j1]
    last = 0.0
    for k, t in enumerate(out):
        last = max(last, t if t is not None else last)
        out[k] = last
    return out  # type: ignore[return-value]


def follow(cap: dict, patch: dict) -> dict:
    """The caption.update patch with the marks it moves (see the module docstring); a patch that sets kt itself
    is left alone, so undo and the timeline's mark drags write exactly what they mean."""
    if "kt" in patch or not isinstance(cap.get("kt"), list):
        return patch
    kt: list[float] | None = [float(t) for t in cap["kt"]]
    if "text" in patch and chars(patch["text"]) != chars(cap.get("text")):
        kt = remap(cap.get("text") or "", patch["text"], kt)
    if kt is not None and "start" in patch:
        s0, s1 = float(cap.get("start") or 0), float(patch["start"])
        e0 = float(cap.get("end") or 0)
        moved = "end" in patch and abs((float(patch["end"]) - e0) - (s1 - s0)) < 1e-3
        if not moved and abs(s1 - s0) > 1e-4:
            kt = [round(max(0.0, t + s0 - s1), 3) for t in kt]
    return patch if kt == cap["kt"] else {**patch, "kt": kt}


def line_spans(text: str, lines: list[str]) -> list[list[int]]:
    """For each wrapped line, the index in chars(text) of each of its characters. wrap_text keeps the characters
    in order and only drops spaces at line ends and adds breaks, so the walk skips whitespace it cannot find."""
    src = chars(text)
    out: list[list[int]] = []
    j = 0
    for line in lines:
        idx = []
        for ch in line:
            while j < len(src) and src[j] != ch and src[j].isspace():
                j += 1
            if j < len(src) and src[j] == ch:
                idx.append(j)
                j += 1
        out.append(idx)
        if j < len(src) and src[j] == "\n":
            j += 1
    return out


def overlay_events(cap: dict, kt: list[float], lines: list[str], lefts: list[float],
                   width: Callable[[str], float], pad: float, place: str, times: str, style: str,
                   restore: str) -> list[str]:
    r"""One layer-4 Dialogue per wrapped line: the whole caption in the sung colours, the other lines see-through,
    clipped to the left of the wipe front. The front runs over each character while it is sung, as a chain of
    \t(\clip) steps (each step starts from where the one before left the clip).

    lefts: each line's left edge in script pixels; pad: how far the outline reaches past the glyphs;
    restore: the \1a\3a tags that make the target line visible again after a see-through one."""
    from .captions import ass_text  # captions.py imports this module

    start_cs = max(0, round(float(cap["start"]) * 100))

    def ms(t: float) -> int:
        return max(0, round((float(cap["start"]) + t) * 1000) - start_cs * 10)

    out = []
    for n, idx in enumerate(line_spans(cap.get("text") or "", lines)):
        if not idx:
            continue
        line, x0 = lines[n], lefts[n]
        left = round(x0 - pad)
        steps = [f"\\clip({left},0,{left},1080)"]
        for k, j in enumerate(idx):
            a, b = kt[j], max(kt[j + 1], kt[j] + MIN_STEP)
            front = x0 + width(line[:k + 1]) + (pad if k == len(idx) - 1 else 0)
            steps.append(f"\\t({ms(a)},{ms(b)},\\clip({left},0,{round(front)},1080))")
        body = "\\N".join(
            (f"{{{restore}}}" if m == n else "{\\alpha&HFF&}") + ass_text(other) for m, other in enumerate(lines))
        out.append(f"Dialogue: 4,{times},{style},,0,0,0,,{{{place}{''.join(steps)}}}{body}")
    return out
