"""Caption spelling check against the user's word list (settings glossary, CP-12): mirror of web/js/lib/glossary.js.

Each line of the list is a correct term, optionally followed by "=" and wrong forms already seen, separated by
commas: "黃政文=黃正文,黃正雯". Text positions count characters (code points); the browser counts UTF-16 units,
which only differs for characters outside the BMP (emoji).
"""
from __future__ import annotations

import re

from .jscompat import js_number

WORD = re.compile(r"[A-Za-z0-9]")
MIN_NEAR = 3  # shorter terms are checked only through their listed wrong forms


def parse_glossary(text: str | None) -> list[dict]:
    """[{term, wrong: [...]}] for each line that names a term."""
    entries = []
    for line in re.split(r"\r?\n", str(text or "")):
        head, *rest = line.split("=")
        term = head.strip()
        if not term:
            continue
        wrong = [w.strip() for w in re.split(r"[,，、]", "=".join(rest))]
        entries.append({"term": term, "wrong": [w for w in wrong if w and w != term]})
    return entries


def _bounded(text: str, i: int, n: int, term: str) -> bool:
    """A run that stands as a whole word where the term starts or ends with a Latin letter or digit."""
    return (not (WORD.match(term[0]) and i > 0 and WORD.match(text[i - 1]))
            and not (WORD.match(term[n - 1]) and i + n < len(text) and WORD.match(text[i + n])))


def glossary_hits(text: str | None, entries: list[dict]) -> list[dict]:
    """[{found, term}], each pair once: a listed wrong form in the text, or a run as long as a term (three
    characters or more) that differs from it in one character or only in letter case. A run that is itself one
    of the terms is left alone."""
    src = str(text or "")
    terms = {e["term"] for e in entries}
    hits: dict[tuple[str, str], dict] = {}

    def add(found: str, term: str) -> None:
        if found not in terms:
            hits[(found, term)] = {"found": found, "term": term}

    for entry in entries:
        term = entry["term"]
        for w in entry["wrong"]:
            if w in src:
                add(w, term)
        n = len(term)
        if n < MIN_NEAR:
            continue
        low = term.lower()
        for i in range(len(src) - n + 1):
            run = src[i:i + n]
            if run == term or not _bounded(src, i, n, term):
                continue
            low_run = run.lower()
            diff = 0
            for j in range(n):
                if diff >= 2:
                    break
                if low_run[j] != low[j]:
                    diff += 1
            if diff <= 1:
                add(run, term)
    return list(hits.values())


def fix_hits(text: str | None, hits: list[dict]) -> str:
    """The text with every hit replaced by its term, in hit order."""
    out = str(text or "")
    for h in hits:
        out = out.replace(h["found"], h["term"])
    return out


def caption_hits(captions: list[dict], text: str | None) -> list[dict]:
    """[{caption, term, fix}] for the captions shown in the video (typoWarning in dialogs/export-check.js): each
    suspect run `term` and the glossary term `fix` it should be. Empty when the glossary is."""
    entries = parse_glossary(text)
    if not entries:
        return []
    return [{"caption": c.get("id"), "term": h["found"], "fix": h["term"]}
            for c in captions
            if str(c.get("text") or "").strip() and js_number(c, "end") > js_number(c, "start")
            and not c.get("disabled")
            for h in glossary_hits(c.get("text"), entries)]
