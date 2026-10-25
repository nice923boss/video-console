"""Karaoke captions (CP-13): each character of a caption turns the style's highlight colour as its voice-over
says it. The times come from the voice media's charTimes (chartimes.py), moved onto the timeline by the voice
clip's start, trim and speed. web/js/lib/karaoke.js lights the preview the same way.

A caption is drawn plainly when its style has no highlight, it has no enabled voice clip, the voice has no
times yet (the backfill adds them) or was made for other words (the caption was edited after voicing)."""
from __future__ import annotations

from . import captions  # captions.py calls back into this module


def voice_starts(project: dict, cap: dict) -> list[float] | None:
    """Timeline time each character of the caption's trimmed text lights up, never earlier than the one
    before it; punctuation and spaces light with the character before them (leading ones with the first)."""
    text = (cap.get("text") or "").strip()
    clip = next((a for a in project.get("audio") or [] if a.get("captionId") == cap.get("id")
                 and not a.get("disabled")), None)
    media = (project.get("media") or {}).get(clip.get("mediaId")) if clip else None
    times = (media or {}).get("charTimes")
    if not times or len(times) != len(text) or ((media.get("source") or {}).get("text") or "") != text:
        return None
    start, trim = float(clip.get("start") or 0), float(clip.get("in") or 0)
    speed = float(clip.get("speed") or 1) or 1.0
    starts: list[float | None] = []
    last = None
    for t in times:
        if t is not None:
            at = start + (float(t) - trim) / speed
            last = at if last is None else max(last, at)
        starts.append(last)
    first = next((t for t in starts if t is not None), None)
    if first is None:
        return None
    return [first if t is None else t for t in starts]


def karaoke_text(project: dict, cap: dict, lines: list[str], fills: str) -> str | None:
    r"""ASS text of the caption's wrapped `lines` with a \k tag before every character, or None to draw it
    plainly. fills: the \1c\1a (lit) and \2c\2a (unlit) override tags; \k shows a syllable in the
    secondary colour until its time comes, then in the primary colour."""
    starts = voice_starts(project, cap)
    if starts is None:
        return None
    text = (cap.get("text") or "").strip()
    begin = float(cap.get("start") or 0)

    def cs(t: float) -> int:
        return max(0, round((t - begin) * 100))

    # Starts never go back, so neither do the centiseconds; each \k is the gap to the next character.
    marks = [cs(t) for t in starts]
    marks.append(max(marks[-1], cs(float(cap.get("end") or 0))))
    out = [f"{{{fills}\\k{marks[0]}}}"]
    j = 0
    for ch in "\n".join(lines):
        # wrap_text keeps the characters in order; it only drops spaces at line ends and adds line breaks.
        # A dropped space keeps its \k with nothing to show, since every \k counts towards the next start.
        while j < len(text) and text[j] != ch and text[j].isspace():
            out.append(f"{{\\k{marks[j + 1] - marks[j]}}}")
            j += 1
        if j < len(text) and text[j] == ch:
            out.append(f"{{\\k{marks[j + 1] - marks[j]}}}{captions.ass_text(ch)}")
            j += 1
        else:
            out.append(captions.ass_text(ch))  # a line break wrap_text added
    return "".join(out)
