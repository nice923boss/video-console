"""Bring a cloud video to the project's aspect ratio (CL-07).

Some models offer only a few shapes (LTX 2.5 text-to-video: 16:9 and 9:16), so a 1:1 or 4:5 project gets a
video of another shape back. It is center-cropped before it reaches the library, so the board, the preview
and the export all show the frame the project asked for.
"""
from __future__ import annotations

from math import gcd
from pathlib import Path
from typing import Callable

from .media import Media

TOLERANCE = 0.05  # a smaller aspect difference stays; export scales it


def _shape(width: int, height: int) -> str:
    g = gcd(width, height)
    a, b = width // g, height // g
    return f"{a}:{b}" if max(a, b) <= 32 else f"{width}×{height}"


def to_aspect(media: Media, path: Path, width: int, height: int, note: Callable[[str], None]) -> Path:
    """Center-crop `path` to width:height when its aspect is off by more than TOLERANCE. Returns the file to
    keep: `path` itself, or an H.264 MP4 beside it that replaces it."""
    info = media.probe(path)
    w, h = info.get("width") or 0, info.get("height") or 0
    if not (w and h and width and height):
        return path
    target = width / height
    if abs((w / h) / target - 1) <= TOLERANCE:
        return path
    cw, ch = (round(h * target), h) if w / h > target else (w, round(w / target))
    dst = path.with_name(f"{path.stem}.crop.mp4")
    args = ["-i", str(path), "-vf", f"crop={cw - cw % 2}:{ch - ch % 2}", *media.encoder_args("libx264", "high")]
    args += ["-c:a", "aac", "-b:a", "192k"] if info["hasAudio"] else ["-an"]
    try:
        media.run([*args, "-movflags", "+faststart", str(dst)], timeout=1800)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    path.unlink(missing_ok=True)
    note(f"供應商回傳 {_shape(w, h)} 的影片，已置中裁成專案的 {_shape(width, height)}")
    return dst
