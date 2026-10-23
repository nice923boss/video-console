"""Project frame rate: 29.97 is kept as entered and exported as NTSC 30000/1001; new projects default to 1920x1080."""
from __future__ import annotations

from pathlib import Path

import pytest

from vc.export import export_options, frame_rate, plan_export, prepare_clips
from vc.ops import SETTINGS_FIELDS, apply_ops, blank_shot
from vc.presets import ASPECTS
from vc.settings import DEFAULTS
from vc.util import AppError


def make_project(fps: float) -> dict:
    shot = blank_shot("s_aaaaaaaa", {})
    shot["duration"] = 2.0
    return {"id": "p_test0000", "name": "t", "settings": {"width": 320, "height": 180, "fps": fps},
            "shots": [shot], "takes": {}, "media": {}, "audio": [], "captions": [], "overlays": [], "layers": [],
            "tracks": {}, "adjustments": []}


def test_fps_field_keeps_29_97_and_whole_rates_as_int() -> None:
    conv = SETTINGS_FIELDS["fps"]
    assert conv(29.97) == 29.97 and conv("29.97") == 29.97
    assert conv(30) == 30 and isinstance(conv(30.0), int) and conv("25") == 25
    assert conv(0) == 1 and conv(120) == 60
    with pytest.raises(AppError):
        conv("abc")


def test_project_update_stores_29_97() -> None:
    p = make_project(24)
    undo = apply_ops(p, [{"op": "project.update", "patch": {"settings": {"fps": 29.97}}}], {})
    assert p["settings"]["fps"] == 29.97
    apply_ops(p, undo, {})
    assert p["settings"]["fps"] == 24


def test_frame_rate_writes_ntsc_as_a_fraction() -> None:
    assert frame_rate(29.97) == "30000/1001"
    assert frame_rate(24.0) == "24" and frame_rate(25) == "25" and frame_rate(30) == "30"


@pytest.mark.parametrize("fps, rate", [(29.97, "30000/1001"), (24, "24")])
def test_export_graph_uses_the_project_rate(tmp_path: Path, fps: float, rate: str) -> None:
    p = make_project(fps)
    clips = prepare_clips(p, tmp_path)
    plan = plan_export(p, tmp_path, clips, export_options(p, {}, None))
    assert f",fps={rate},format=yuv420p" in plan["graph"]
    assert f":r={rate}" in " ".join(" ".join(i) for i in plan["inputs"])


def test_new_projects_default_to_1920x1080() -> None:
    assert (DEFAULTS["defaults"]["width"], DEFAULTS["defaults"]["height"]) == (1920, 1080)
    # Picking 16:9 selects the same size, so the dialog marks 1920 x 1080 as the default.
    [wide] = [a for a in ASPECTS if a["id"] == "16:9"]
    assert wide["sizes"][0] == [1920, 1080]
