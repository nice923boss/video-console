"""Blur and mosaic boxes over a shot's picture (GR-12): validation, their place in the export graph and a render."""
from __future__ import annotations

from pathlib import Path

import pytest

from test_alpha_overlay import FFMPEG, VID, make_project, pixel
from vc.export import export_options, plan_export, prepare_clips, render
from vc.media import Media
from vc.ops import apply_ops
from vc.util import AppError

BLUR = {"x": 0.25, "y": 0.2, "w": 0.5, "h": 0.5, "mode": "blur", "strength": 0.5}


def _graph(p: dict, root: Path) -> str:
    return plan_export(p, root, prepare_clips(p, root), export_options(p, {}, {}), None, [])["graph"]


def test_masks_are_checked_filled_and_undone() -> None:
    p = make_project()
    back = apply_ops(p, [{"op": "shot.update", "id": "s_aaaaaaaa",
                          "patch": {"masks": [{"x": 0.9, "w": 0.3, "mode": "mosaic", "strength": 3}]}}], {})
    assert p["shots"][0]["masks"] == [{"x": 0.7, "y": 0.35, "w": 0.3, "h": 0.3, "mode": "mosaic", "strength": 1.0}]
    for bad in ("box", [1], [{"mode": "pixel"}], [{"w": "wide"}], [BLUR] * 9):
        with pytest.raises(AppError):
            apply_ops(p, [{"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"masks": bad}}], {})
    apply_ops(p, back, {})
    assert not p["shots"][0].get("masks")


def test_masks_run_on_the_fitted_frame(tmp_path: Path) -> None:
    (tmp_path / f"media/takes/{VID}.mp4").parent.mkdir(parents=True)
    (tmp_path / f"media/takes/{VID}.mp4").write_bytes(b"x")
    p = make_project()
    p["shots"][0]["masks"] = [BLUR, {**BLUR, "mode": "mosaic", "strength": 1.0}]
    graph = _graph(p, tmp_path)
    assert "trim=duration=3.000[v0m]" in graph
    # 320x180 frame: the box is 160x90 at (80, 36); blur radius round(0.5 * 180 * 0.04) = 4,
    # mosaic block round(180 * 0.06) = 11, so 15x9 blocks blown up to 165x99 and cut back to the box.
    assert "[v0m]split[v0b0][v0c0]" in graph
    assert "[v0c0]crop=160:90:80:36,boxblur=4:2[v0f0]" in graph
    assert "[v0b0][v0f0]overlay=80:36[v0m0]" in graph
    assert "[v0c1]crop=160:90:80:36,scale=15:9:flags=area,scale=165:99:flags=neighbor,crop=160:90:0:0[v0f1]" in graph
    assert "[v0b1][v0f1]overlay=80:36[v0]" in graph
    assert graph.index("scale=320:180") < graph.index("[v0m]split")
    p["shots"][0]["disabled"] = True
    assert "split" not in _graph(p, tmp_path)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_render_blurs_and_pixelates_only_inside_the_boxes(tmp_path: Path) -> None:
    media = Media(FFMPEG)
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    # Black on the left, white on the right: a hard edge at x = 160 for the boxes to soften.
    media.run(["-f", "lavfi", "-i", "color=c=black:s=320x180:r=24:d=3,drawbox=x=160:y=0:w=160:h=180:color=white:t=fill",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(takes / f"{VID}.mp4")])
    p = make_project()
    p["shots"][0]["masks"] = [{"x": 0.25, "y": 0.1, "w": 0.5, "h": 0.3, "mode": "blur", "strength": 1.0},
                              {"x": 0.25, "y": 0.6, "w": 0.5, "h": 0.3, "mode": "mosaic", "strength": 1.0}]
    result = render(media, p, tmp_path, export_options(p, {}, {"quality": "draft"}), "libx264")
    out = tmp_path / result["file"]
    assert pixel(media, out, 1.5, 157, 90)[0] < 40  # between the boxes the edge stays hard
    assert pixel(media, out, 1.5, 163, 90)[0] > 215
    assert 50 < pixel(media, out, 1.5, 157, 45)[0] < 205  # blurred: grey on both sides of the edge
    assert 50 < pixel(media, out, 1.5, 163, 45)[0] < 205
    # Mosaic blocks of 11 pixels from x = 80: 157..167 is one block, one colour across the edge.
    left, right = pixel(media, out, 1.5, 158, 135)[0], pixel(media, out, 1.5, 166, 135)[0]
    assert abs(left - right) < 25 and 40 < left < 215
