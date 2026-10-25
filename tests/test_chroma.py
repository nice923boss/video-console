"""Colour key on picture-in-picture clips (GR-09): validation, its place in the export graph and a render."""
from __future__ import annotations

from pathlib import Path

import pytest

from test_alpha_overlay import FFMPEG, VID, make_project, pixel, with_overlay
from vc.export import export_options, plan_export, prepare_clips, render
from vc.media import Media
from vc.ops import apply_ops
from vc.util import AppError

PICTURE = {"kind": "image", "file": "media/import/m_alpha0000.png"}


def test_chroma_is_checked_filled_and_undone() -> None:
    p = with_overlay(make_project(), PICTURE)
    back = apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa",
                          "patch": {"chroma": {"on": True, "color": "#00FF00", "similarity": 5}}}], {})
    assert p["overlays"][0]["chroma"] == {"on": True, "color": "#00ff00", "similarity": 1.0, "blend": 0.1}
    for bad in ("green", {"color": "green"}, {"color": "#0f0"}, {"blend": "soft"}):
        with pytest.raises(AppError):
            apply_ops(p, [{"op": "overlay.update", "id": "o_aaaaaaaa", "patch": {"chroma": bad}}], {})
    apply_ops(p, back, {})
    assert "chroma" not in p["overlays"][0]


def test_chroma_key_runs_on_the_clip_before_it_is_scaled(tmp_path: Path) -> None:
    for name in (f"media/takes/{VID}.mp4", PICTURE["file"]):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    p = with_overlay(make_project(), PICTURE)
    p["overlays"][0]["chroma"] = {"on": True, "color": "#00ff00", "similarity": 0.2, "blend": 0.05}
    graph = plan_export(p, tmp_path, prepare_clips(p, tmp_path), export_options(p, {}, {}), None, [])["graph"]
    key = "chromakey=color=0x00ff00:similarity=0.200:blend=0.050"
    assert key in graph and graph.count("chromakey") == 1
    assert graph.index("setpts=PTS-STARTPTS+0.000/TB,") < graph.index(key) < graph.index("scale=320:-2")
    p["overlays"][0]["chroma"]["on"] = False
    graph = plan_export(p, tmp_path, prepare_clips(p, tmp_path), export_options(p, {}, {}), None, [])["graph"]
    assert "chromakey" not in graph


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_render_keys_out_the_green_screen(tmp_path: Path) -> None:
    media = Media(FFMPEG)
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "color=c=red:s=320x180:r=24:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               str(takes / f"{VID}.mp4")])
    # A green screen with a yellow box in the middle.
    (tmp_path / "media" / "import").mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "color=c=0x00ff00:s=320x180,drawbox=x=110:y=60:w=100:h=60:color=yellow:t=fill",
               "-frames:v", "1", str(tmp_path / PICTURE["file"])])
    p = with_overlay(make_project(), PICTURE)
    p["overlays"][0]["chroma"] = {"on": True, "color": "#00ff00", "similarity": 0.15, "blend": 0.1}
    result = render(media, p, tmp_path, export_options(p, {}, {"quality": "draft"}), "libx264")
    out = tmp_path / result["file"]
    r, g, b = pixel(media, out, 1.5, 20, 20)  # the green is gone: the red shot shows through
    assert r > 180 and g < 70 and b < 70
    r, g, b = pixel(media, out, 1.5, 160, 90)  # the yellow box stays
    assert r > 180 and g > 180 and b < 90
