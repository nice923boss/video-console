"""Small shared helpers: ids, time, JSON persistence, path safety."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class AppError(Exception):
    """Error with a user-facing (zh-TW) message and an HTTP-ish status."""

    def __init__(self, message: str, status: int = 400, detail: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def new_id(prefix: str) -> str:
    """Short random id such as 's_k3j9x2q1ab'."""
    return prefix + "_" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(10))


def now() -> float:
    return round(time.time(), 3)


def random_seed() -> int:
    return secrets.randbelow(2**32)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON through a temp file + replace so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def replace_with_retry(src: str | Path, dst: str | Path, attempts: int = 8) -> None:
    """os.replace that tolerates short-lived locks (antivirus, indexer) on Windows."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def safe_filename(name: str, fallback: str = "file", limit: int = 120) -> str:
    """Strip characters Windows rejects and any directory parts from a user-supplied name."""
    base = os.path.basename(str(name).replace("\\", "/"))
    base = _UNSAFE_CHARS.sub("_", base).strip(" .")
    if not base:
        base = fallback
    stem, ext = os.path.splitext(base)
    if len(base) > limit:
        base = stem[: max(1, limit - len(ext))] + ext
    return base


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def deep_merge(base: dict, patch: dict) -> dict:
    """Return a new dict with patch merged into base recursively (patch wins)."""
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * 6
    return value[:3] + "…" + value[-4:]


def fmt_seconds(sec: float) -> str:
    sec = max(0, int(round(sec)))
    if sec < 60:
        return f"{sec} 秒"
    minutes, s = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes} 分 {s:02d} 秒"
    hours, m = divmod(minutes, 60)
    return f"{hours} 小時 {m:02d} 分"


def guess_ext(data: bytes, default: str = ".bin") -> str:
    """Detect common image/video container types from magic bytes."""
    head = data[:16]
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if head.startswith(b"GIF8"):
        return ".gif"
    return default


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def kind_of(filename: str) -> str | None:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return None
