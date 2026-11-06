"""The on-screen text of a p5 animation segment, and the edits the APP keeps for it.

A page from the p5-comfyui-animation skill (index.html?id=M04, three.html?id=M01) draws segment M04 with
`SCENES.M04 = ...` in one of its scripts. The texts listed here are the string literals inside that statement
that hold CJK or full-width characters: the words a viewer reads. English-only strings are left out, they are
mostly font names, colours and options.

Edits never touch the user's files. A media item keeps them in item["p5"]["edits"] as
{file, at, old, new}: the literal's offset in the source text and its raw spelling before and after, quotes
included. The renderer (p5render.py) serves the page with those literals swapped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .util import AppError

CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_$-]{1,40}$")
SCRIPT_RE = re.compile(r"<script\b[^>]*?\bsrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)
PAGE_SUFFIXES = (".html", ".htm")
MAX_EDITS = 200
MAX_TEXT = 1000
MAX_RAW = 4000
MAX_PATH = 1000
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v"}
OPEN, CLOSE = "([{", ")]}"
# A line that starts with one of these continues the statement above it.
CONTINUES = ".)]},?:+-*/%&|=<>"
BLANK = re.compile(r"[ \t\r\n]*")


def segment_guess(name: str) -> str:
    """M04.webm -> M04: the skill names each render after its segment id."""
    stem = Path(str(name or "")).stem.strip()
    return stem if SEGMENT_RE.match(stem) else ""


# ---------------------------------------------------------------- validation (media.update "p5")
def _edit(v: Any) -> dict:
    if not isinstance(v, dict):
        raise AppError("p5 文字修改格式不正確")
    file, at, old, new = v.get("file"), v.get("at"), v.get("old"), v.get("new")
    if (not isinstance(file, str) or not file or len(file) > 300 or "\\" in file or ".." in file.split("/")
            or file.startswith("/")):
        raise AppError("p5 文字修改的檔案路徑不正確")
    if not isinstance(at, int) or isinstance(at, bool) or at < 0:
        raise AppError("p5 文字修改的位置不正確")
    for raw in (old, new):
        if not isinstance(raw, str) or len(raw) < 2 or len(raw) > MAX_RAW or raw[0] not in "'\"`" or raw[-1] != raw[0]:
            raise AppError("p5 文字修改內容格式不正確")
    return {"file": file, "at": at, "old": old, "new": new}


def clean_link(v: Any) -> dict:
    """media.update's "p5" field: {page, segment, edits}."""
    if not isinstance(v, dict):
        raise AppError("p5 原始碼連結格式不正確")
    page, segment, edits = v.get("page"), v.get("segment"), v.get("edits") or []
    if not isinstance(page, str) or not page.lower().endswith(PAGE_SUFFIXES) or len(page) > MAX_PATH:
        raise AppError("p5 頁面路徑不正確（要是 .html 檔）")
    if not isinstance(segment, str) or not SEGMENT_RE.match(segment):
        raise AppError("p5 片段代號不正確（例如 M04）")
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        raise AppError("p5 文字修改格式不正確")
    return {"page": page, "segment": segment, "edits": [_edit(e) for e in edits]}


# ---------------------------------------------------------------- string literals
def decode(raw: str) -> str:
    """The text a JS string literal (quotes included) stands for."""
    body, out, i = raw[1:-1], [], 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        n = body[i + 1:i + 2]
        i += 2
        if n in ESCAPES:
            out.append(ESCAPES[n])
        elif n == "0" and not body[i:i + 1].isdigit():
            out.append("\0")
        elif n == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", body[i:i + 2]):
            out.append(chr(int(body[i:i + 2], 16)))
            i += 2
        elif n == "u" and body[i:i + 1] == "{" and "}" in body[i:]:
            end = body.index("}", i)
            try:
                out.append(chr(int(body[i + 1:end], 16)))
            except ValueError:
                out.append("u")
                continue
            i = end + 1
        elif n == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", body[i:i + 4]):
            out.append(chr(int(body[i:i + 4], 16)))
            i += 4
        elif n == "\r":
            i += body[i:i + 1] == "\n"  # line continuation
        elif n != "\n":
            out.append(n)
    return "".join(out)


def encode(text: str, quote: str) -> str:
    """text as a JS literal in the given quote, the inverse of decode."""
    s = text.replace("\\", "\\\\").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if quote == "`":
        s = s.replace("`", "\\`").replace("${", "\\${").replace("\r", "\\r")
    else:
        s = s.replace(quote, "\\" + quote).replace("\n", "\\n").replace("\r", "\\r")
    return f"{quote}{s}{quote}"


def _skip_quoted(text: str, i: int) -> tuple[int, bool]:
    """i is at an opening ' or "; returns (index after the closing one or the end of the line, whether it closed)."""
    q, n = text[i], len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == q:
            return i + 1, True
        elif c == "\n":
            return i, False
        else:
            i += 1
    return n, False


def _skip_template(text: str, i: int) -> tuple[int, bool]:
    """i is at an opening backtick; returns (index after the closing one, whether it is a plain literal: closed,
    with no ${...} inside)."""
    n, has_expr = len(text), False
    i += 1
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1, not has_expr
        elif c == "$" and text[i + 1:i + 2] == "{":
            has_expr, depth = True, 1
            i += 2
            while i < n and depth:
                c = text[i]
                if c in "'\"":
                    i = _skip_quoted(text, i)[0]
                    continue
                if c == "`":
                    i = _skip_template(text, i)[0]
                    continue
                depth += (c == "{") - (c == "}")
                i += 1
        else:
            i += 1
    return n, False


def statement_literals(text: str, start: int) -> tuple[int, list[tuple[int, int]]]:
    """Scan the statement that starts at `start` (just past "SCENES.X ="). Returns where it ends and the
    (start, end) spans of its plain string literals (no ${...} inside)."""
    i, n, depth, seen = start, len(text), 0, False
    spans: list[tuple[int, int]] = []
    while i < n:
        c = text[i]
        if c in "'\"`":
            end, plain = _skip_template(text, i) if c == "`" else _skip_quoted(text, i)
            if plain:
                spans.append((i, end))
            i, seen = end, True
        elif text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
        elif c in OPEN:
            depth, seen = depth + 1, True
            i += 1
        elif c in CLOSE:
            if depth == 0:
                return i, spans
            depth -= 1
            i += 1
        elif c == ";" and depth == 0:
            return i + 1, spans
        elif c == "\n" and depth == 0 and seen:
            j = BLANK.match(text, i + 1).end()
            # Something at column 0 starts the next statement (automatic semicolon insertion) or the next
            # segment's comment; an indented line, or one starting with an operator, continues this one.
            if j >= n or (text.rfind("\n", i, j) + 1 == j and (text.startswith("//", j) or text[j] not in CONTINUES)):
                return i, spans
            i += 1
        else:
            if not c.isspace():
                seen = True
            i += 1
    return n, spans


def _head_re(segment: str) -> re.Pattern:
    seg = re.escape(segment)
    return re.compile(rf"(?m)^[ \t]*(?:window\.)?SCENES\s*(?:\.\s*{seg}(?![\w$])|\[\s*(['\"`]){seg}\1\s*\])\s*=(?!=)")


# ---------------------------------------------------------------- page and its scripts
def check_page(page: str | Path) -> Path:
    path = Path(str(page or "")).expanduser()
    if not path.is_absolute() or path.suffix.lower() not in PAGE_SUFFIXES:
        raise AppError("請選擇 p5 動畫的 .html 頁面（完整路徑）")
    if not path.is_file():
        raise AppError(f"找不到 p5 頁面：{path}（可能已被移動或刪除）", 404)
    return path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise AppError(f"{path.name} 不是 UTF-8 編碼，無法讀取文字", 415) from None
    except OSError as err:
        raise AppError(f"無法讀取 {path.name}：{err}", 500) from None


def page_scripts(page: Path, html: str) -> list[str]:
    """Local scripts the page loads, as paths relative to its folder (CDN scripts are left out)."""
    root = page.parent.resolve()
    out: list[str] = []
    for _, src in SCRIPT_RE.findall(html):
        src = src.strip().split("#", 1)[0].split("?", 1)[0]
        if not src or re.match(r"^([a-z][a-z0-9+.-]*:|//)", src, re.I):
            continue
        path = (root / unquote(src).lstrip("/")).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue  # outside the page folder: the renderer serves only that folder
        if path.is_file() and rel not in out:
            out.append(rel)
    return out


def find_segment(page: Path, segment: str) -> dict:
    """The file and statement that draw `segment`: {file, text, start, end, line}. Raises when no script has it."""
    if not SEGMENT_RE.match(segment or ""):
        raise AppError("p5 片段代號不正確（例如 M04）")
    html = _read(page)
    head = _head_re(segment)
    for rel in [page.name, *page_scripts(page, html)]:
        text = html if rel == page.name else _read(page.parent / rel)
        m = head.search(text)
        if m:
            end, _ = statement_literals(text, m.end())
            return {"file": rel, "text": text, "start": m.end(), "end": end, "line": text.count("\n", 0, m.start()) + 1}
    raise AppError(f"在 {page.name} 載入的程式裡找不到 SCENES.{segment}（片段代號對嗎？）", 404)


def _literals(found: dict) -> list[dict]:
    text, out = found["text"], []
    _, spans = statement_literals(text, found["start"])
    for a, b in spans:
        raw = text[a:b]
        if CJK.search(raw):
            out.append({"at": a, "raw": raw, "line": text.count("\n", 0, a) + 1})
    return out


def _place(found: dict, lits: list[dict], edits: list[dict]) -> tuple[dict[int, dict], list[dict]]:
    """Match stored edits to the literals in the current source: {at: edit} and the ones no longer there.
    An edit whose literal moved (the source was edited above it) follows it when its old spelling is unique."""
    by_at = {lit["at"]: lit for lit in lits}
    placed: dict[int, dict] = {}
    stale: list[dict] = []
    for e in edits:
        lit = by_at.get(e["at"]) if e["file"] == found["file"] else None
        if lit is None or lit["raw"] != e["old"]:
            same = [x for x in lits if x["raw"] == e["old"] and x["at"] not in placed]
            lit = same[0] if e["file"] == found["file"] and len(same) == 1 else None
        if lit is None or lit["at"] in placed:
            stale.append(e)
        else:
            placed[lit["at"]] = {**e, "at": lit["at"]}
    return placed, stale


def read_texts(page: str | Path, segment: str, edits: list[dict] | None = None) -> dict:
    """What the inspector lists: every text of the segment, with the stored edits shown."""
    path = check_page(page)
    found = find_segment(path, segment)
    lits = _literals(found)
    placed, stale = _place(found, lits, edits or [])
    texts = []
    for lit in lits:
        e = placed.get(lit["at"])
        original = decode(lit["raw"])
        texts.append({"file": found["file"], "at": lit["at"], "line": lit["line"], "original": original,
                      "text": decode(e["new"]) if e else original, "edited": e is not None})
    return {"page": str(path), "segment": segment, "file": found["file"], "line": found["line"], "texts": texts,
            "stale": [decode(e["old"]) for e in stale]}


def plan_edits(page: str | Path, segment: str, wanted: Any) -> list[dict]:
    """The edits for wanted [{at, text}] (positions from read_texts): a literal whose text is back to the
    original drops out, so the list holds only real changes."""
    if not isinstance(wanted, list) or len(wanted) > MAX_EDITS:
        raise AppError("p5 文字格式不正確")
    found = find_segment(check_page(page), segment)
    by_at = {lit["at"]: lit for lit in _literals(found)}
    edits: dict[int, dict] = {}
    for w in wanted:
        if not isinstance(w, dict) or not isinstance(w.get("text"), str) or not isinstance(w.get("at"), int):
            raise AppError("p5 文字格式不正確")
        text = w["text"]
        if len(text) > MAX_TEXT or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
            raise AppError(f"p5 文字太長或含控制字元（最多 {MAX_TEXT} 字）")
        lit = by_at.get(w["at"])
        if lit is None:
            raise AppError("原始碼已改過，找不到這段文字，請重新讀取文字", 409)
        if text == decode(lit["raw"]):
            edits.pop(lit["at"], None)
            continue
        edits[lit["at"]] = {"file": found["file"], "at": lit["at"], "old": lit["raw"], "new": encode(text, lit["raw"][0])}
    return [edits[k] for k in sorted(edits)]


def substitutions(page: str | Path, segment: str, edits: list[dict]) -> dict[str, str]:
    """{relative file: source text with the edits applied} for the renderer to serve in place of the file."""
    if not edits:
        return {}
    found = find_segment(check_page(page), segment)
    placed, stale = _place(found, _literals(found), edits)
    if stale:
        lost = "、".join(f"「{decode(e['old'])}」" for e in stale[:3])
        raise AppError(f"原始碼已改過，找不到 {lost}，請重新讀取文字再重算", 409)
    text = found["text"]
    for at in sorted(placed, reverse=True):
        e = placed[at]
        text = text[:at] + e["new"] + text[at + len(e["old"]):]
    return {found["file"]: text}
