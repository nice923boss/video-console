"""Platform presets in the export (EX-03): an output size scaled at the very end and a video bitrate cap."""
from __future__ import annotations

from pathlib import Path

import pytest

from test_timeline_keys import make_project, with_video
from vc.export import TO_709, export_options, native_size, plan_export, prepare_clips
from vc.media import Media
from vc.settings import SettingsStore
from vc.util import AppError


def test_size_and_bitrate_default_to_the_project_and_the_quality_rate() -> None:
    opts = export_options(make_project(), {}, {})
    assert opts["size"] is None and opts["maxrate"] == 0


def test_size_is_checked_and_made_even() -> None:
    p = make_project()
    assert export_options(p, {}, {"size": [1081, 1921]})["size"] == [1080, 1920]
    for bad in ([8, 8], [1920, 9000], ["a", 2], [1920]):
        with pytest.raises(AppError):
            export_options(p, {}, {"size": bad})
    assert export_options(p, {}, {"maxrate": 999999})["maxrate"] == 200000
    assert export_options(p, {}, {"maxrate": -5})["maxrate"] == 0


def _vout(plan: dict) -> str:
    return next(line for line in plan["graph"].split(";\n") if line.endswith("[vout]"))


def test_output_size_scales_the_finished_picture(tmp_path: Path) -> None:
    p = with_video(make_project())
    (tmp_path / p["takes"]["t_vid00000"]["file"]).parent.mkdir(parents=True)
    (tmp_path / p["takes"]["t_vid00000"]["file"]).write_bytes(b"x")
    plain = plan_export(p, tmp_path, prepare_clips(p, tmp_path), export_options(p, {}, {}), None, [])
    assert (plain["width"], plain["height"]) == (320, 180) and "scale=" not in _vout(plain).replace(TO_709, "")

    opts = export_options(p, {}, {"size": [180, 320], "fit": "cover"})
    tall = plan_export(p, tmp_path, prepare_clips(p, tmp_path), opts, None, [])
    assert (tall["width"], tall["height"]) == (180, 320)
    # The scale runs after captions and fades, so burnt-in text is laid out at the project size.
    assert _vout(tall).endswith(f"force_original_aspect_ratio=increase:flags=lanczos,crop=180:320,setsar=1,{TO_709}[vout]")
    same = plan_export(p, tmp_path, prepare_clips(p, tmp_path), export_options(p, {}, {"size": [320, 180]}), None, [])
    assert "scale=" not in _vout(same).replace(TO_709, "")


def test_output_size_of_the_project_shape_is_laid_out_at_that_size(tmp_path: Path) -> None:
    p = with_video(make_project())
    (tmp_path / p["takes"]["t_vid00000"]["file"]).parent.mkdir(parents=True)
    (tmp_path / p["takes"]["t_vid00000"]["file"]).write_bytes(b"x")
    big, opts = native_size(p, export_options(p, {}, {"size": [640, 360]}))
    assert (big["settings"]["width"], big["settings"]["height"]) == (640, 360) and opts["size"] is None
    assert (p["settings"]["width"], p["settings"]["height"]) == (320, 180)
    plan = plan_export(big, tmp_path, prepare_clips(big, tmp_path), opts, None, [])
    assert (plan["width"], plan["height"]) == (640, 360) and "scale=" not in _vout(plan).replace(TO_709, "")
    assert "scale=640:360" in plan["graph"]
    # Another shape still scales the finished picture.
    tall = export_options(p, {}, {"size": [180, 320]})
    assert native_size(p, tall) == (p, tall)


def test_bitrate_cap_reaches_every_encoder() -> None:
    assert "-maxrate" not in Media.encoder_args("libx264", "high")
    x264 = Media.encoder_args("libx264", "high", 8000)
    assert x264[x264.index("-maxrate") + 1] == "8000k" and x264[x264.index("-bufsize") + 1] == "16000k"
    assert "-crf" in x264
    nv = Media.encoder_args("h264_nvenc", "standard", 12000)
    assert "-cq" in nv and nv[nv.index("-maxrate") + 1] == "12000k"
    amf = Media.encoder_args("h264_amf", "high", 4000)
    assert amf[amf.index("-rc") + 1] == "vbr_peak" and amf[amf.index("-b:v") + 1] == "4000k"
    assert "cqp" in Media.encoder_args("h264_amf", "high")
    qsv = Media.encoder_args("h264_qsv", "high", 4000)
    assert qsv[qsv.index("-b:v") + 1] == "4000k" and "-global_quality" not in qsv


def test_preset_choice_is_kept_only_when_known(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "s.json")
    store.update({"export": {"preset": "reels"}})
    assert store.section("export")["preset"] == "reels"
    store.update({"export": {"preset": "tiktok"}})
    assert store.section("export")["preset"] == ""
    store.update({"export": {"resolution": "2160"}})
    assert store.section("export")["resolution"] == "2160"
    store.update({"export": {"resolution": "8k"}})
    assert store.section("export")["resolution"] == ""
