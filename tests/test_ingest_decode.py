"""Video import reads the file once for the filmstrip and the waveform (PF-01, GAPS #143)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vc.media import Media, find_ffmpeg
from vc.takes import stage_file
from vc.util import AppError

needs_ffmpeg = pytest.mark.skipif(find_ffmpeg() is None, reason="ffmpeg is not installed")


def make_clip(path: Path, sound: bool = True) -> Path:
    audio = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest"] if sound else []
    subprocess.run([find_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25:d=2", *audio,
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def jpeg_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    i = 2
    while i < len(data):
        marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in (0xC0, 0xC2):
            return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(data[i + 5 : i + 7], "big")
        i += 2 + length
    raise AssertionError("no frame header")


def refuse(*_a: object, **_k: object) -> None:
    raise AssertionError("the separate decode ran")


@needs_ffmpeg
def test_one_run_writes_the_filmstrip_and_the_waveform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = make_clip(tmp_path / "in.mp4")
    peaks, duration, level = Media(find_ffmpeg()).waveform(clip)
    monkeypatch.setattr(Media, "strip", refuse)
    monkeypatch.setattr(Media, "waveform", refuse)
    info = stage_file(Media(find_ffmpeg()), tmp_path, clip, "media/import", "m_clip0000")
    assert info["strip"] == "media/thumbs/m_clip0000_strip.jpg" and info["wave"] == "media/thumbs/m_clip0000_wave.json"
    assert jpeg_size(tmp_path / info["strip"]) == (8 * 160, 90)
    # The same envelope as the separate waveform run (lavfi's sine plays at 1/8 of full scale).
    wave = json.loads((tmp_path / info["wave"]).read_text(encoding="utf-8"))
    assert wave == {"peaks": peaks, "duration": duration, "level": level}
    assert duration == pytest.approx(2.0, abs=0.05) and level == pytest.approx(0.125, abs=0.01)


@needs_ffmpeg
def test_each_is_tried_alone_when_the_shared_run_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Media, src: Path, strip: Path, *_a: object, **_k: object) -> None:
        strip.write_bytes(b"half")  # a partial file must not stand in for the filmstrip
        raise AppError("boom", 500)

    monkeypatch.setattr(Media, "strip_and_waveform", broken)
    info = stage_file(Media(find_ffmpeg()), tmp_path, make_clip(tmp_path / "in.mp4"), "media/import", "m_clip0000")
    assert jpeg_size(tmp_path / info["strip"]) == (8 * 160, 90)
    assert json.loads((tmp_path / info["wave"]).read_text(encoding="utf-8"))["duration"] == pytest.approx(2.0, abs=0.05)


@needs_ffmpeg
def test_a_silent_video_only_gets_the_filmstrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Media, "strip_and_waveform", refuse)
    info = stage_file(Media(find_ffmpeg()), tmp_path, make_clip(tmp_path / "in.mp4", sound=False),
                      "media/import", "m_clip0000")
    assert info["strip"] and info.get("wave") is None and not info["hasAudio"]
