"""Generated pictures carry no lettering: titles and subtitles belong in the editor, not in the model's output.

Z-Image draws whatever Chinese text a prompt asks for, and the local templates run at cfg 1 where the negative
prompt has no effect, so a script direction such as "5. 標題字卡浮現" came out as a fake title card. Directions like
that are dropped before the job is queued, and every prompt sent to an engine carries a no-text clause.
"""
from __future__ import annotations

import re

# Lettering laid over the picture; a clause naming one is an editing note, not something to draw.
_DIRECTION_RE = re.compile(
    r"字卡|字幕|標題|片名|標語|title\s*cards?|intertitles?|subtitles?|captions?|on-screen\s+text|text\s+overlays?"
    r"|lower\s+thirds?",
    re.IGNORECASE,
)
_CLAUSE_RE = re.compile(r".+?(?:[，,。；;！!？?]+\s*|\.\s+|$)")  # "1. " splits off too, so it never keeps a line
_LETTER_RE = re.compile(r"[^\W\d_]")
_TRAILING = "，,。；;！!？?、 \t"

GUARD = "No text anywhere in the image: no subtitles, captions, title cards, letters, numbers, watermarks or logos."
NEGATIVE_TERMS = ("text", "subtitles", "captions", "title card", "letters", "watermark", "logo")


def strip_text_directions(prompt: str) -> tuple[str, list[str]]:
    """The prompt without the clauses that ask for titles or subtitles, and the clauses that were dropped."""
    lines: list[str] = []
    dropped: list[str] = []
    for line in prompt.split("\n"):
        if not _DIRECTION_RE.search(line):
            lines.append(line)
            continue
        kept = ""
        for clause in _CLAUSE_RE.findall(line):
            if _DIRECTION_RE.search(clause):
                dropped.append(clause.strip().rstrip(_TRAILING))
            else:
                kept += clause
        kept = kept.rstrip(_TRAILING)
        if _LETTER_RE.search(kept):  # a bare "3." left over is dropped with its line
            lines.append(kept)
    return "\n".join(lines).strip(), dropped


def with_guard(prompt: str) -> str:
    """The prompt as sent to an engine; an empty prompt (a picture-only video) stays empty."""
    return f"{prompt}\n{GUARD}" if prompt else prompt


def with_text_negative(negative: str) -> str:
    """The negative prompt plus the no-text terms it does not name yet."""
    missing = [t for t in NEGATIVE_TERMS if not re.search(rf"\b{re.escape(t)}\b", negative, re.IGNORECASE)]
    return ", ".join([negative, *missing] if negative else missing)


def dropped_note(dropped: list[str]) -> str:
    quoted = "、".join(f"「{d}」" for d in dropped)
    return f"畫面不畫字卡與字幕（請在剪輯台加），已略過提示詞裡的：{quoted}"
