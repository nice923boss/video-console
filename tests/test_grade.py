"""Adjustment layers and their colour grade: validation, the pixel model, edits and the export graph."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from test_timeline_keys import FFMPEG, VID, with_video
from vc.export import active_adjustments, export_options, plan_export, prepare_clips, render, shot_grades
from vc.grade import build_cube, clean_grade, default_grade, grader, is_neutral
from vc.media import Media
from vc.opfields import blank_shot
from vc.ops import apply_ops
from vc.util import AppError


def graded(**changes: object) -> dict:
    g = default_grade()
    for key, value in changes.items():
        if "_" in key:
            wheel, field = key.split("_")
            g[wheel][field] = value
        else:
            g[key] = value
    return clean_grade(g)


def test_clean_grade_fills_defaults_and_clamps() -> None:
    assert clean_grade(None) == default_grade() == clean_grade({})
    g = clean_grade({"temp": 99999, "hue": -500, "shadows": {"r": 3, "sat": -1}, "mix": 0.5})
    assert g["temp"] == 10000 and g["hue"] == -180 and g["mix"] == 0.5
    assert g["shadows"] == {"r": 1.0, "g": 0.0, "b": 0.0, "sat": 0.0, "bright": 0.0}
    assert g["highlights"] == {"r": 0.0, "g": 0.0, "b": 0.0, "sat": 1.0, "bright": 0.0}
    for bad in ("warm", [], {"exposure": 1}, {"global": {"lift": 1}}, {"tint": "5"}, {"hue": float("nan")}):
        with pytest.raises(AppError):
            clean_grade(bad)


def test_neutral_grade_leaves_pixels_alone() -> None:
    assert is_neutral(default_grade()) and is_neutral(graded(temp=8000, mix=0))
    assert not is_neutral(graded(tint=10))
    apply = grader(default_grade())
    for px in ((0.0, 0.0, 0.0), (0.2, 0.5, 0.9), (1.0, 1.0, 1.0)):
        assert apply(*px) == pytest.approx(px, abs=1e-3)
    cube = build_cube(default_grade(), size=3).splitlines()
    assert cube[1] == "LUT_3D_SIZE 3" and len(cube) == 4 + 27
    assert cube[4] == "0.000000 0.000000 0.000000" and cube[5] == "0.500000 0.000000 0.000000"  # red runs fastest
    assert cube[-1] == "1.000000 1.000000 1.000000"


def test_each_control_moves_the_picture_the_right_way() -> None:
    gray = (0.5, 0.5, 0.5)
    r, g, b = grader(graded(temp=10000))(*gray)
    assert r > 0.6 and b < 0.4  # warmer
    r, g, b = grader(graded(tint=100))(*gray)
    assert g < r and g < b  # magenta
    assert grader(graded(shadows_bright=0.5))(0.05, 0.05, 0.05)[0] > 0.15  # lifted blacks
    assert grader(graded(highlights_bright=-0.5))(0.95, 0.95, 0.95)[0] < 0.75
    assert grader(graded(midtones_bright=0.5))(*gray)[0] > 0.6
    r, g, b = grader(graded(global_r=0.5, global_g=-0.25, global_b=-0.25))(*gray)
    assert r > g and r > b  # red puck on the global wheel
    assert grader(graded(global_sat=0))(0.8, 0.2, 0.2) == pytest.approx((0.3276,) * 3, abs=1e-3)
    # Shadow saturation only touches the dark end.
    apply = grader(graded(shadows_sat=0))
    dark, bright = apply(0.2, 0.05, 0.05), apply(0.95, 0.8, 0.8)
    assert dark[0] - dark[1] < 0.03 and bright[0] - bright[1] > 0.12
    half = grader(graded(temp=10000, mix=0.5))(*gray)
    full = grader(graded(temp=10000))(*gray)
    assert half[0] == pytest.approx((full[0] + 0.5) / 2, abs=1e-6)


def make_project() -> dict:
    shots = []
    for sid in ("s_aaaaaaaa", "s_bbbbbbbb"):
        shot = blank_shot(sid, {})
        shot["duration"] = 4.0
        shots.append(shot)
    return {"id": "p_test0000", "name": "t", "settings": {"width": 320, "height": 180, "fps": 24},
            "shots": shots, "takes": {}, "media": {}, "audio": [], "captions": [], "overlays": [], "layers": [],
            "tracks": {}, "adjustments": []}


def test_adjustment_layer_edits_undo_and_follow_their_shot() -> None:
    p = make_project()
    item = {"id": "adj_warm00000", "name": "暖色", "start": 4.5, "length": 2.0, "grade": {"temp": 7000}}
    undo = apply_ops(p, [{"op": "adjust.add", "item": item}], {})
    [adj] = p["adjustments"]
    assert adj["grade"]["temp"] == 7000 and adj["grade"]["shadows"]["sat"] == 1.0
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "adjust.update", "id": "adj_warm00000", "patch": {"layer": "l_x0000000"}}], {})
    back = apply_ops(p, [{"op": "adjust.update", "id": "adj_warm00000",
                          "patch": {"grade": {"tint": 30}, "length": 0}}], {})
    assert adj["grade"]["tint"] == 30 and adj["grade"]["temp"] == 5000 and adj["length"] == 0.1
    apply_ops(p, back, {})
    assert adj["grade"]["temp"] == 7000 and adj["length"] == 2.0
    apply_ops(p, [{"op": "project.update", "patch": {"tracks": {"adjustHidden": True}}}], {})
    assert p["tracks"]["adjustHidden"] is True

    # Lengthening the first shot carries the layer sitting on the second one.
    apply_ops(p, [{"op": "linked", "ops": [{"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"duration": 5.0}}]}], {})
    assert p["adjustments"][0]["start"] == 5.5
    apply_ops(p, undo, {})
    assert p["adjustments"] == []


def test_export_graph_grades_each_adjustment_span(tmp_path: Path) -> None:
    p = make_project()
    p["adjustments"] = [
        {"id": "adj_a0000000", "start": 1.0, "length": 2.0, "hidden": False, "grade": graded(temp=9000)},
        {"id": "adj_b0000000", "start": 6.0, "length": 9.0, "hidden": False, "grade": graded(hue=40)},
        {"id": "adj_c0000000", "start": 0.0, "length": 8.0, "hidden": True, "grade": graded(hue=40)},
        {"id": "adj_d0000000", "start": 0.0, "length": 8.0, "hidden": False, "grade": default_grade()},
    ]
    spans = active_adjustments(p, 8.0)
    assert [(s, e) for s, e, _ in spans] == [(1.0, 3.0), (6.0, 8.0)]  # hidden and neutral layers drop out
    clips = prepare_clips(p, tmp_path)
    opts = export_options(p, {}, {"burnCaptions": True})
    luts = [(s, e, f"media/cache/x-adj{i}.cube") for i, (s, e, _) in enumerate(spans)]
    graph = plan_export(p, tmp_path, clips, opts, "media/cache/x.ass", luts)["graph"]
    assert "lut3d=file=media/cache/x-adj0.cube:interp=tetrahedral:enable='between(t,1.000,3.000)'[adj0]" in graph
    assert "[adj0]lut3d=file=media/cache/x-adj1.cube" in graph and "enable='between(t,6.000,8.000)'[adj1]" in graph
    # Captions are burnt in after the grade, so they keep their own colours.
    assert graph.index("[adj1]") < graph.index("ass=filename=")
    p["tracks"]["adjustHidden"] = True
    assert active_adjustments(p, 8.0) == []


def test_graded_captions_are_burnt_in_before_the_grade(tmp_path: Path) -> None:
    p = make_project()
    p["settings"]["gradeCaptions"] = True
    p["adjustments"] = [{"id": "adj_a0000000", "start": 1.0, "length": 2.0, "hidden": False, "grade": graded(hue=40)}]
    clips = prepare_clips(p, tmp_path)
    opts = export_options(p, {}, {"burnCaptions": True})
    graph = plan_export(p, tmp_path, clips, opts, "media/cache/x.ass", [(1.0, 3.0, "media/cache/x-adj0.cube")])["graph"]
    assert graph.count("ass=filename=") == 1
    assert graph.index("ass=filename=media/cache/x.ass[caps]") < graph.index("[caps]lut3d=")
    # Without captions there is nothing to grade, so the flag changes nothing.
    plain = plan_export(p, tmp_path, clips, opts, None, [(1.0, 3.0, "media/cache/x-adj0.cube")])["graph"]
    assert "ass=" not in plain and "[caps]" not in plain


def test_shot_grade_is_checked_and_undone() -> None:
    p = make_project()
    back = apply_ops(p, [{"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"grade": {"temp": 7000}}}], {})
    shot = p["shots"][0]
    assert shot["grade"]["temp"] == 7000 and shot["grade"]["global"]["sat"] == 1.0
    with pytest.raises(AppError):
        apply_ops(p, [{"op": "shot.update", "id": "s_aaaaaaaa", "patch": {"grade": {"temp": "warm"}}}], {})
    apply_ops(p, back, {})
    assert "grade" not in shot


def graded_shots(tmp_path: Path) -> dict:
    """Four shots: a graded video, a graded but disabled video, a video with a neutral grade, a graded slate."""
    p = make_project()
    p["shots"].append(blank_shot("s_cccccccc", {}))
    with_video(p)
    p["shots"].append(blank_shot("s_dddddddd", {}))
    warm = graded(temp=9000)
    p["shots"][0]["grade"] = warm
    p["shots"][1].update(grade=warm, disabled=True)
    p["shots"][2]["grade"] = default_grade()
    p["shots"][3]["grade"] = warm
    (tmp_path / "media" / "takes").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / "takes" / f"{VID}.mp4").write_bytes(b"x")
    return p


def test_shot_grade_goes_on_its_clip_before_the_fit(tmp_path: Path) -> None:
    p = graded_shots(tmp_path)
    clips = prepare_clips(p, tmp_path)
    assert [(i, g["temp"]) for i, g in shot_grades(clips)] == [(0, 9000)]  # disabled, neutral and slate drop out
    opts = export_options(p, {}, {})
    graph = plan_export(p, tmp_path, clips, opts, None, [], {0: "media/cache/x-shot0.cube"})["graph"]
    # Graded before scale and pad, so the letterbox bars stay black.
    assert "[0:v]setpts=PTS-STARTPTS,setparams=colorspace=bt709,lut3d=file=media/cache/x-shot0.cube:interp=tetrahedral,scale=" in graph
    assert graph.count("lut3d=") == 1
    assert "lut3d" not in plan_export(p, tmp_path, clips, opts, None, [])["graph"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_render_grades_only_the_graded_shot(tmp_path: Path) -> None:
    media = Media(FFMPEG)
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "color=c=gray:size=240x180:rate=24:duration=6", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", str(takes / f"{VID}.mp4")])
    p = with_video(make_project())
    p["takes"][VID]["hasAudio"] = False
    p["shots"][0]["grade"] = graded(temp=10000, shadows_bright=0.4)
    result = render(media, p, tmp_path, export_options(p, {}, {"quality": "draft"}), "libx264")
    out = tmp_path / result["file"]
    assert not list((tmp_path / "media" / "cache").glob("*.cube"))  # the LUT files are cleaned up

    def pixel(t: float, x: int) -> tuple[int, int, int]:
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1", "-f", "rawvideo",
                              "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return tuple(raw[(90 * 320 + x) * 3:(90 * 320 + x) * 3 + 3])

    warm, plain = pixel(1, 160), pixel(7, 160)
    assert warm[0] - warm[2] > 40 and abs(plain[0] - plain[2]) < 10, (warm, plain)
    assert max(pixel(1, 5)) < 25  # the pillarbox bar beside the 4:3 clip is not lifted by the grade
