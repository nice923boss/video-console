"""Project documents: files kept beside a project for the AI assistant to read, such as the script, the client's
notes or reference pictures. They live in the project's docs/ folder, so a file copied there by hand counts too.
Text is read out of Word, PDF and plain-text files; pictures go to the model as pictures."""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .script import docx_document_text
from .util import IMAGE_EXTS, AppError, new_id

if TYPE_CHECKING:
    from .media import Media

log = logging.getLogger(__name__)

FOLDER = "docs"
TEXT_EXTS = {".txt", ".md", ".srt", ".vtt", ".csv", ".tsv", ".json", ".ass"}
DOC_EXTS = {".docx", ".pdf"}
MAX_FILE = 100 * 1024 ** 2  # bytes of one upload
MAX_LIST = 300  # files one listing returns
IMAGE_WIDTH = 768  # pixels wide a picture is sent at (about 400 tokens on a vision model)
NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
CACHE_SIZE = 16  # extracted texts kept in memory
_cache: OrderedDict[tuple[str, int, int], str] = OrderedDict()
_cache_lock = threading.Lock()


def kind_of(name: str) -> str | None:
    """"text" (read as text), "image", or None (not a document the assistant can use)."""
    ext = os.path.splitext(name)[1].lower()
    if ext in TEXT_EXTS or ext in DOC_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def folder(project_dir: Path) -> Path:
    path = project_dir / FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def clean_name(name: str) -> str:
    """A safe file name for the docs folder, or AppError when its type is not supported."""
    base = NAME_BAD.sub("_", str(name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip().strip(".")
    stem, ext = os.path.splitext(base)
    base = stem[:100].strip() + ext.lower()
    if not stem.strip() or kind_of(base) is None:
        raise AppError("不支援的文件類型（可用 Word、PDF、文字檔、字幕檔與圖片）", 415)
    return base


def listing(project_dir: Path) -> list[dict]:
    """The usable files in the docs folder by name: {name, kind, size, modified}. Hidden files, Office lock files
    (~$...) and other file types are left out."""
    out = []
    for path in sorted(folder(project_dir).iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name.startswith((".", "~$")) or kind_of(path.name) is None:
            continue
        st = path.stat()
        out.append({"name": path.name, "kind": kind_of(path.name), "size": st.st_size, "modified": int(st.st_mtime)})
        if len(out) >= MAX_LIST:
            break
    return out


def find(project_dir: Path, name: str) -> Path:
    path = folder(project_dir) / clean_name(name)
    if not path.is_file():
        raise AppError(f"找不到專案文件「{name}」", 404)
    return path


def save(project_dir: Path, name: str, fill: Callable[[Path], object]) -> dict:
    """Write a new document (fill writes the file); a name already taken gets " (2)", " (3)"... added."""
    name = clean_name(name)
    base = folder(project_dir)
    stem, ext = os.path.splitext(name)
    path, n = base / name, 2
    while path.exists():
        path, n = base / f"{stem} ({n}){ext}", n + 1
    part = base / f".{new_id('up')}.part"
    try:
        fill(part)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return next(d for d in listing(project_dir) if d["name"] == path.name)


def delete(project_dir: Path, name: str) -> None:
    find(project_dir, name).unlink()


def decode(data: bytes) -> str:
    """Plain text in UTF-8 (with or without BOM), UTF-16 with BOM, or Big5 (cp950) as Windows saves it."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf_text(data: bytes) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise AppError("讀取 PDF 需要 PyMuPDF 套件（pip install pymupdf），或先把 PDF 另存成 Word 或文字檔", 501) from None
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            pages = [page.get_text().strip() for page in doc]
    except (RuntimeError, ValueError) as exc:  # PyMuPDF's FileDataError and EmptyFileError are RuntimeErrors
        log.info("PDF unreadable: %s", exc)
        raise AppError("無法讀取這個 PDF 檔（可能損毀或有密碼）") from None
    if not any(pages):
        return "（這個 PDF 沒有文字層，可能是掃描的圖片；請改用文字版，或把頁面存成圖片放進專案文件）"
    return "\n\n".join(f"[第 {i} 頁]\n{t}" for i, t in enumerate(pages, 1) if t)


def text_of(path: Path) -> str:
    """The text of a text document, cached while the file is unchanged."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    data = path.read_bytes()
    ext = path.suffix.lower()
    text = docx_document_text(data) if ext == ".docx" else _pdf_text(data) if ext == ".pdf" else decode(data)
    with _cache_lock:
        _cache[key] = text
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return text


def jpeg_url(media: Media, src: Path, work: Path, width: int = IMAGE_WIDTH, at: float | None = None) -> str:
    """A picture (or a video frame at `at` seconds) as a JPEG data URL `width` pixels wide at most."""
    work.mkdir(parents=True, exist_ok=True)
    dst = work / f"look-{new_id('f')}.jpg"
    try:
        media.thumbnail(src, dst, width=width, at=at)
        data = dst.read_bytes()
    finally:
        dst.unlink(missing_ok=True)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
