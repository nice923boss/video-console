"""A cloud video of another shape is center-cropped to the project's aspect ratio (CL-07)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vc.conform import to_aspect
from vc.media import Media, find_ffmpeg

FFMPEG = find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def clip(path: Path, size: str) -> Path:
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:duration=1:rate=12",
                    "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True)
    return path


@needs_ffmpeg
def test_a_wide_video_is_cropped_to_a_tall_project(tmp_path: Path) -> None:
    media, notes = Media(FFMPEG), []
    src = clip(tmp_path / "out.mp4", "320x180")
    kept = to_aspect(media, src, 1080, 1350, notes.append)
    info = media.probe(kept)
    assert (info["width"], info["height"]) == (144, 180) and info["hasAudio"]
    assert kept != src and not src.exists() and kept.suffix == ".mp4"
    assert notes == ["供應商回傳 16:9 的影片，已置中裁成專案的 4:5"]


@needs_ffmpeg
def test_a_close_enough_video_is_left_alone(tmp_path: Path) -> None:
    media, notes = Media(FFMPEG), []
    src = clip(tmp_path / "out.mp4", "320x184")  # 1.74 against 1.78: under the tolerance
    assert to_aspect(media, src, 1920, 1080, notes.append) == src and src.exists() and notes == []
