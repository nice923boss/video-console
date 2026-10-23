"""Transparent WebM clips (VP9 with alpha, e.g. a p5.js animation rendered with a see-through background) on an
overlay track: import keeps the file as it is and remembers the alpha, export decodes it with libvpx so the
picture below shows through."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vc.export import export_options, plan_export, prepare_clips, render
from vc.media import Media, parse_probe
from vc.ops import blank_shot
from vc.takes import new_media, stage_file

FFMPEG = shutil.which("ffmpeg")
VID = "t_vid00000"

ALPHA_PROBE = """Input #0, matroska,webm, from 'a.webm':
  Duration: 00:00:03.00, start: 0.000000, bitrate: 15 kb/s
  Stream #0:0: Video: vp9 (Profile 0), yuv420p(tv, progressive), 320x240, SAR 1:1 DAR 4:3, 30 fps, 30 tbr, 1k tbn
    Metadata:
      alpha_mode      : 1
      ENCODER         : Lavc62.11.100 libvpx-vp9
"""


def make_project() -> dict:
    shot = blank_shot("s_aaaaaaaa", {})
    shot.update(duration=3.0, activeVideo=VID)
    return {"id": "p_test0000", "name": "t", "settings": {"width": 320, "height": 180, "fps": 24},
            "shots": [shot], "media": {}, "audio": [], "captions": [], "overlays": [], "tracks": {},
            "adjustments": [], "markers": [],
            "takes": {VID: {"id": VID, "kind": "video", "file": f"media/takes/{VID}.mp4", "duration": 3.0,
                            "hasAudio": False, "width": 320, "height": 180, "shot": "s_aaaaaaaa"}},
            "layers": [{"id": "l_top00000", "name": "上方", "hidden": False, "muted": False}]}


def with_overlay(p: dict, media: dict) -> dict:
    p["media"]["m_alpha0000"] = {"id": "m_alpha0000", "duration": 3.0, "hasAudio": False, "width": 320,
                                 "height": 180, **media}
    p["overlays"] = [{"id": "o_aaaaaaaa", "mediaId": "m_alpha0000", "layer": "l_top00000", "start": 0.0, "in": 0.0,
                      "length": 3.0, "x": 0.0, "y": 0.0, "w": 1.0, "opacity": 1.0, "volume": 1.0}]
    return p


def test_probe_sees_the_alpha_tag_only_on_vp8_and_vp9() -> None:
    assert parse_probe(ALPHA_PROBE)["alpha"] is True
    assert parse_probe(ALPHA_PROBE.replace("      alpha_mode      : 1\n", ""))["alpha"] is False
    assert parse_probe(ALPHA_PROBE.replace("Video: vp9 (Profile 0)", "Video: h264 (High)"))["alpha"] is False


def test_export_decodes_a_transparent_clip_with_libvpx(tmp_path: Path) -> None:
    for name in (f"media/takes/{VID}.mp4", "media/import/m_alpha0000.webm", "media/import/m_plain0000.webm"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    p = with_overlay(make_project(), {"kind": "video", "file": "media/import/m_alpha0000.webm", "alphaCodec": "vp9"})
    out = plan_export(p, tmp_path, prepare_clips(p, tmp_path), export_options(p, {}, {}), None, [])
    clip = next(i for i in out["inputs"] if "media/import/m_alpha0000.webm" in i)
    assert clip[-4:] == ["-c:v", "libvpx-vp9", "-i", "media/import/m_alpha0000.webm"]
    plain = with_overlay(make_project(), {"kind": "video", "file": "media/import/m_plain0000.webm"})
    out = plan_export(plain, tmp_path, prepare_clips(plain, tmp_path), export_options(plain, {}, {}), None, [])
    assert "-c:v" not in next(i for i in out["inputs"] if "media/import/m_plain0000.webm" in i)


def pixel(media: Media, video: Path, at: float, x: int, y: int, width: int = 320) -> tuple[int, int, int]:
    proc = subprocess.run([media.exe, "-v", "error", "-ss", str(at), "-i", str(video), "-frames:v", "1",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    at_byte = (y * width + x) * 3
    return tuple(proc.stdout[at_byte:at_byte + 3])


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_transparent_webm_imports_as_is_and_shows_the_picture_below(tmp_path: Path) -> None:
    media = Media(FFMPEG)
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "color=c=red:s=320x180:r=24:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               str(takes / f"{VID}.mp4")])
    # A see-through 320x180 clip with an opaque yellow box in the middle.
    src = tmp_path / "anim.webm"
    media.run(["-f", "lavfi", "-i", "color=c=yellow:s=320x180:r=24:d=3,format=rgba,"
               "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*between(X,110,209)*between(Y,60,119)'", "-c:v", "libvpx-vp9",
               "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(src)])

    staged = stage_file(media, tmp_path, src, "media/import", "m_alpha0000")
    item = new_media(staged, "anim.webm", "m_alpha0000")
    assert item["file"] == "media/import/m_alpha0000.webm" and item["alphaCodec"] == "vp9"

    p = with_overlay(make_project(), item)
    result = render(media, p, tmp_path, export_options(p, {}, {"quality": "draft"}), "libx264")
    out = tmp_path / result["file"]
    r, g, b = pixel(media, out, 1.5, 20, 20)  # outside the box: the red shot shows through
    assert r > 180 and g < 70 and b < 70
    r, g, b = pixel(media, out, 1.5, 160, 90)  # the box itself
    assert r > 180 and g > 180 and b < 90
