"""Upper-track clips that fill the frame (ED-15): sizes past the frame width, cropped by the frame."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_timeline_keys import FFMPEG, JS, make_project, plan, run_js, with_video
from vc.export import export_options, render
from vc.media import Media
from vc.ops import apply_ops

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def fill(width: int, height: int) -> dict:
    body = "console.log(JSON.stringify(m.fillFrame(data.media, 320, 180)));"
    return run_js(JS / "lib" / "sequence.js", body, {"media": {"width": width, "height": height}})


@node
def test_fill_covers_the_frame_centered() -> None:
    assert fill(180, 320) == {"x": 0, "y": pytest.approx(-1.0802, abs=1e-4), "w": 1}  # tall: crop top and bottom
    assert fill(420, 180) == {"x": pytest.approx(-0.1563, abs=1e-4), "y": 0, "w": pytest.approx(1.3125)}
    assert fill(320, 180) == {"x": 0, "y": 0, "w": 1}


def overlay_project() -> dict:
    p = with_video(make_project())
    p["media"]["m_pic0000a"] = {"id": "m_pic0000a", "kind": "image", "file": "media/pic.png", "width": 480, "height": 180}
    p["layers"] = [{"id": "l_top00000", "name": "", "hidden": False, "muted": False}]
    p["overlays"] = [{"id": "o_aaaaaaaa", "mediaId": "m_pic0000a", "layer": "l_top00000", "start": 0.0, "in": 0.0,
                      "length": 2.0, "x": 0.0, "y": 0.0, "w": 0.5, "opacity": 1.0, "volume": 1.0}]
    return p


def test_sizes_past_the_frame_are_kept_and_bounded() -> None:
    p = overlay_project()
    apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa", "patch": {"x": -0.25, "y": -1.08, "w": 1.5}}], {})
    assert {k: p["overlays"][0][k] for k in ("x", "y", "w")} == {"x": -0.25, "y": -1.08, "w": 1.5}
    apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa", "patch": {"w": 4.5, "x": -5}}], {})
    assert (p["overlays"][0]["w"], p["overlays"][0]["x"]) == (4, -4)  # clamped like every number field


def test_export_places_a_wide_clip_left_of_the_frame(tmp_path: Path) -> None:
    p = overlay_project()
    p["overlays"][0].update(x=-0.25, w=1.5)
    (tmp_path / "media" / "pic.png").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "pic.png").write_bytes(b"x")
    g = plan(p, tmp_path)["graph"]
    assert "scale=480:-2" in g and "overlay=x=-80:y=0" in g


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_render_crops_the_clip_at_the_frame(tmp_path: Path) -> None:
    media = Media(FFMPEG)
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=24:duration=4", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", str(takes / "t_vid00000.mp4")])
    media.run(["-f", "lavfi", "-i", "color=c=red:size=480x180", "-frames:v", "1", str(tmp_path / "media" / "pic.png")])
    p = overlay_project()
    p["takes"]["t_vid00000"]["hasAudio"] = False
    p["overlays"][0].update(x=-0.25, w=1.5)  # 480 px wide, 80 px past each side
    result = render(media, p, tmp_path, export_options(p, {}, {"quality": "draft"}), "libx264")
    out = tmp_path / result["file"]
    assert media.probe(out)["width"] == 320
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    for x in (2, 160, 317):  # both edges are the clip, not the blue picture underneath
        r, g, b = raw[(90 * 320 + x) * 3:(90 * 320 + x) * 3 + 3]
        assert r > 180 and b < 80, (x, r, g, b)
