"""輸出音訊: the sequence's mix alone as WAV or MP3, planned without the picture and rendered by the export job."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from test_server import App, Client, app, client, needs_ffmpeg, project  # noqa: F401  (server fixtures)
from test_timeline_keys import VID, make_project, plan, with_video
from vc.export import export_options, plan_audio_export, prepare_clips, render
from vc.media import Media, find_ffmpeg
from vc.util import AppError


def overlays(p: dict) -> dict:
    """A video picture-in-picture with sound and a still one on the same track."""
    p["media"]["m_vid00000"] = {"id": "m_vid00000", "kind": "video", "file": f"media/takes/{VID}.mp4",
                                "duration": 6.0, "hasAudio": True, "width": 320, "height": 180}
    p["media"]["m_pic00000"] = {"id": "m_pic00000", "kind": "image", "file": f"media/takes/{VID}.mp4",
                                "width": 320, "height": 180}
    p["layers"] = [{"id": "l_top00000", "name": "", "hidden": False, "muted": False}]
    p["overlays"] = [
        {"id": "o_vid00000", "mediaId": "m_vid00000", "layer": "l_top00000", "start": 1.0, "in": 0.0, "length": 2.0,
         "x": 0.1, "y": 0.1, "w": 0.3, "opacity": 1.0, "volume": 0.5},
        {"id": "o_pic00000", "mediaId": "m_pic00000", "layer": "l_top00000", "start": 4.0, "length": 2.0,
         "x": 0.1, "y": 0.1, "w": 0.3, "opacity": 1.0}]
    return p


def test_format_option_falls_back_to_mp4() -> None:
    p = make_project()
    assert export_options(p, {}, {"format": "wav"})["format"] == "wav"
    assert export_options(p, {}, {"format": "flac"})["format"] == "mp4"
    assert export_options(p, {}, {})["format"] == "mp4"


def test_audio_plan_opens_only_what_is_heard(tmp_path: Path) -> None:
    p = overlays(with_video(make_project()))
    p["shots"][1]["disabled"] = True
    video = plan(p, tmp_path)
    out = plan_audio_export(p, tmp_path, prepare_clips(p, tmp_path), export_options(p, {}, {"format": "wav"}))
    assert "[vout]" in video["graph"] and "[vout]" not in out["graph"]
    assert ":v]" not in out["graph"] and "overlay=" not in out["graph"]
    # The first shot's sound and the video overlay's; the disabled shot and the still are not opened.
    assert len(out["inputs"]) == 2 and len(video["inputs"]) == 4
    assert out["total"] == video["total"]
    assert "[0:a]" in out["graph"] and "[1:a]" in out["graph"] and "amix=inputs=2" in out["graph"]
    assert "amix=inputs=2" in video["graph"]


def test_audio_plan_without_any_sound_is_silence(tmp_path: Path) -> None:
    p = with_video(make_project())
    out = plan_audio_export(p, tmp_path, prepare_clips(p, tmp_path),
                            export_options(p, {}, {"format": "mp3", "clipAudio": False}))
    assert out["inputs"][-1][-1].startswith("anullsrc") and "[aout]" in out["graph"]


@needs_ffmpeg
@pytest.mark.parametrize(("fmt", "codec"), [("wav", "pcm_s16le"), ("mp3", "mp3")])
def test_render_writes_sound_only(tmp_path: Path, fmt: str, codec: str) -> None:
    media = Media(find_ffmpeg())
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6", "-f", "lavfi",
               "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-shortest", str(takes / f"{VID}.mp4")])
    p = overlays(with_video(make_project()))
    p["shots"][0]["trimOut"] = 2.0
    p["shots"][1]["trimOut"] = 3.0
    result = render(media, p, tmp_path, export_options(p, {}, {"format": fmt}))
    path = tmp_path / result["file"]
    info = media.probe(path)
    assert path.suffix == f".{fmt}" and result["name"].endswith(f".{fmt}")
    assert info["hasAudio"] and not info["hasVideo"] and info["acodec"] == codec
    assert info["duration"] == pytest.approx(5.0, abs=0.1)
    assert not [f for f in (tmp_path / "media" / "cache").iterdir() if f.name.startswith("export-")]


def test_loudness_option_is_off_unless_a_known_target() -> None:
    p = make_project()
    assert export_options(p, {}, {})["loudness"] is None
    assert export_options(p, {}, {"loudness": -14})["loudness"] == -14.0
    assert export_options(p, {}, {"loudness": "-16"})["loudness"] == -16.0
    for bad in (-20, "loud", [1]):
        with pytest.raises(AppError):
            export_options(p, {}, {"loudness": bad})


def integrated_lufs(path: Path, *input_args: str) -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP) of a file, as ffmpeg's ebur128 filter reports them."""
    proc = subprocess.run([find_ffmpeg(), "-hide_banner", "-nostats", *input_args, "-i", str(path),
                           "-af", "ebur128=peak=true", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    summary = proc.stderr.rsplit("Summary:", 1)[-1]
    return (float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1)),
            float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1)))


@needs_ffmpeg
@pytest.mark.parametrize(("fmt", "target"), [("wav", -16.0), ("mp4", -14.0)])
def test_render_normalises_loudness(tmp_path: Path, fmt: str, target: float) -> None:
    """EX-01: the exported mix lands on the target (the quiet 440 Hz tone needs about +10 dB, applied as one gain)."""
    media = Media(find_ffmpeg())
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6", "-f", "lavfi",
               "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-shortest", str(takes / f"{VID}.mp4")])
    p = with_video(make_project())
    p["shots"][0]["trimOut"] = 3.0
    p["shots"][1]["trimOut"] = 3.0
    progress: list[float] = []
    opts = export_options(p, {}, {"format": fmt, "loudness": target, "quality": "draft"})
    result = render(media, p, tmp_path, opts, "libx264", on_progress=progress.append)
    before, _ = integrated_lufs(takes / f"{VID}.mp4")
    after, peak = integrated_lufs(tmp_path / result["file"])
    assert result["loudness"]["measured"] == pytest.approx(before, abs=0.3)
    assert result["loudness"]["linear"] is True
    assert after == pytest.approx(target, abs=0.5) and peak <= -0.9
    assert media.probe(tmp_path / result["file"])["duration"] == pytest.approx(6.0, abs=0.1)
    assert progress == sorted(progress) and progress[-1] == 1.0
    assert not [f for f in (tmp_path / "media" / "cache").iterdir() if f.name.startswith("export-")]


@needs_ffmpeg
def test_silent_mix_is_left_alone(tmp_path: Path) -> None:
    media = Media(find_ffmpeg())
    p = make_project()  # no takes: slates only, and anullsrc for the sound
    result = render(media, p, tmp_path, export_options(p, {}, {"format": "wav", "loudness": -14}))
    assert result["loudness"] == {"target": -14.0, "measured": None, "linear": False}
    assert (tmp_path / result["file"]).is_file()


def test_export_job_refuses_an_unknown_format(client: Client, project: dict) -> None:  # noqa: F811
    client.json("POST", "/api/jobs", {"kind": "export", "pid": project["id"],
                                      "payload": {"options": {"format": "flac"}}}, status=400)
    client.json("POST", "/api/jobs", {"kind": "export", "pid": project["id"],
                                      "payload": {"options": {"loudness": -20}}}, status=400)


@needs_ffmpeg
def test_export_job_lists_the_sound_file(app: App, client: Client, project: dict) -> None:  # noqa: F811
    pid = project["id"]
    client.json("POST", f"/api/projects/{pid}/ops",
                {"ops": [{"op": "shot.update", "id": project["shots"][0]["id"], "patch": {"duration": 1.0}}]})
    job = client.json("POST", "/api/jobs", {"kind": "export", "pid": pid,
                                            "payload": {"options": {"format": "wav"}}})["job"]
    assert job["title"].startswith("輸出音訊")
    done = client.wait_job(job["id"])
    assert done["status"] == "done", done["error"]
    [entry] = client.json("GET", f"/api/projects/{pid}/exports")
    assert entry["kind"] == "audio" and entry["name"].endswith(".wav")
    assert (app.store.project_dir(pid) / entry["path"]).read_bytes()[:4] == b"RIFF"
