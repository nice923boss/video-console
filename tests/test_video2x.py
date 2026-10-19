"""Video2X upscaling of ComfyUI videos: the 1080P size, the commands, the exit-crash tolerance, the checkbox and
method settings, the job step and the standalone upscale job; also 1080P projects generating at 720P."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from test_comfy import app, client, fake, project, submit_image  # noqa: F401  (shared fixtures)
from test_server import Client, needs_ffmpeg
from vc import video2x
from vc.app import App
from vc.media import find_ffmpeg
from vc.takes import TAKE_DIR, commit_takes, new_take, stage_file
from vc.util import AppError, new_id


class Ctl:
    """Just the job-control hooks the upscale step uses."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.steps: list[str | None] = []

    def check(self) -> None:
        pass

    def cancelled(self) -> bool:
        return False

    def progress(self, frac: float, message: str | None = None) -> None:
        self.steps.append(message)

    def note(self, text: str) -> None:
        self.notes.append(text)


def make_clip(path: Path, size: str = "864x480") -> Path:
    subprocess.run([find_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:d=0.5:r=24",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def fake_video2x(*lines: str, code: int) -> list[str]:
    """A child process that prints like Video2X (progress redrawn with \\r) and exits with `code`."""
    script = "import sys\n" + "".join(f"sys.stdout.write({line!r})\n" for line in lines) + \
        f"sys.stdout.flush()\nsys.exit({code})\n"
    return [sys.executable, "-c", script]


def test_target_size_keeps_the_shape_with_a_1080_short_side() -> None:
    assert video2x.target_size(864, 480) == (1944, 1080)  # LTX-Video's 480P frame for a 16:9 project
    assert video2x.target_size(768, 448) == (1852, 1080)
    assert video2x.target_size(480, 864) == (1080, 1944)
    assert video2x.target_size(1920, 1080) is None
    assert video2x.target_size(0, 0) is None


def test_commands_for_both_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "video2x.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(video2x, "EXE", exe)
    cmd = video2x.command(Path("in.mp4"), Path("out.mp4"), 1944, 1080)
    assert cmd[0] == str(exe)
    assert cmd[cmd.index("-p") + 1] == "libplacebo"
    assert (cmd[cmd.index("-w") + 1], cmd[cmd.index("-h") + 1]) == ("1944", "1080")
    assert cmd[cmd.index("--libplacebo-shader") + 1] == "anime4k-v4-a"
    assert cmd[cmd.index("--pix-fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("--log-level") + 1] == "info"  # the level that prints the success line

    cmd = video2x.command(Path("in.mp4"), Path("out-x4.mp4"), 1944, 1080, "realesrgan")
    assert cmd[cmd.index("-p") + 1] == "realesrgan"
    assert cmd[cmd.index("-s") + 1] == "4"  # Real-ESRGAN refuses -w/-h
    assert cmd[cmd.index("--realesrgan-model") + 1] == "realesrgan-plus"
    assert "-w" not in cmd and "--libplacebo-shader" not in cmd

    monkeypatch.setattr(video2x, "EXE", tmp_path / "missing.exe")
    with pytest.raises(AppError, match="找不到 Video2X") as exc:
        video2x.command(Path("in.mp4"), Path("out.mp4"), 1944, 1080)
    assert exc.value.status == 503


def test_a_crash_after_the_success_line_still_counts_as_done() -> None:
    progress = "\r\x1b[Kframe=1/3 (33.3%); fps=0.4", "\r\x1b[Kframe=3/3 (100.0%); fps=0.4\n"
    frames: list[tuple[int, int]] = []
    video2x.run(fake_video2x(*progress, "[info] Video processed successfully\n", code=0xC0000005 - 2 ** 32),
                lambda: False, lambda done, total: frames.append((done, total)))
    assert frames == [(1, 3), (3, 3)]

    with pytest.raises(AppError, match=r"結束代碼 0xc0000005）：\[error\] vulkan lost") as exc:
        video2x.run(fake_video2x(*progress, "[error] vulkan lost\n", code=0xC0000005 - 2 ** 32), lambda: False)
    assert exc.value.status == 500

    with pytest.raises(AppError, match="結束代碼 0x1"):
        video2x.run(fake_video2x(code=1), lambda: False)


def test_checkbox_and_method_mark_comfy_video_jobs(app: App, client: Client, project: dict, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    done = client.wait_job(submit_image(client, project)["id"], timeout=30)
    assert done["status"] == "done", done["error"]
    pid, sid = project["id"], project["shots"][0]["id"]
    request = {"workflow": "ltxv-2b-i2v"}
    assert app.settings.section("video2x") == {"enabled": False, "method": "anime4k"}
    assert "upscale1080" not in app.runners.prepare("video", pid, sid, request)["payload"]

    assert app.settings.update({"video2x": {"enabled": 1}})["video2x"] == {"enabled": True, "method": "anime4k"}
    monkeypatch.setattr(video2x, "EXE", tmp_path / "missing" / "video2x.exe")
    with pytest.raises(AppError, match="找不到 Video2X"):
        app.runners.prepare("video", pid, sid, request)

    exe = tmp_path / "video2x.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(video2x, "EXE", exe)
    job = app.runners.prepare("video", pid, sid, request)
    assert job["payload"]["upscale1080"] == "anime4k"
    assert "生成後用 Video2X（Anime4K）放大到 1080P，再放上時間軸" in job["notes"]
    assert "upscale1080" not in app.runners.prepare("image", pid, sid, {"prompt": "清晨漁港"})["payload"]

    assert app.settings.update({"video2x": {"method": "realesrgan"}})["video2x"] == \
        {"enabled": True, "method": "realesrgan"}
    job = app.runners.prepare("video", pid, sid, request)
    assert job["payload"]["upscale1080"] == "realesrgan"
    assert "生成後用 Video2X（Real-ESRGAN）放大到 1080P，再放上時間軸" in job["notes"]
    assert "Real-ESRGAN 較慢，5 秒的片段約要 5 到 6 分鐘" in job["notes"]
    assert app.settings.update({"video2x": {"method": "waifu2x"}})["video2x"]["method"] == "anime4k"


@needs_ffmpeg
def test_upscale_step_swaps_in_the_1080p_clip_or_keeps_the_original(app: App, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    media = app.runners._media()
    clip = make_clip(tmp_path / "clip.mp4")
    methods: list[str] = []

    def scale_with_ffmpeg(media: object, src: Path, dst: Path, width: int, height: int, method: str,
                          cancelled: object, on_frames: object) -> None:
        methods.append(method)
        on_frames(5, 10)
        subprocess.run([find_ffmpeg(), "-y", "-v", "error", "-i", str(src), "-vf", f"scale={width}:{height}",
                        "-pix_fmt", "yuv420p", str(dst)], check=True)

    monkeypatch.setattr(video2x, "upscale", scale_with_ffmpeg)
    ctl, source = Ctl(), {"backend": "comfy"}
    [out] = app.runners._upscale_videos(ctl, [clip], source, "realesrgan")
    info = media.probe(out)
    assert out != clip and (info["width"], info["height"]) == (1944, 1080)
    assert methods == ["realesrgan"] and source["upscaler"] == "Video2X Real-ESRGAN"
    assert ctl.steps == ["用 Video2X（Real-ESRGAN）放大到 1944×1080",
                         "用 Video2X（Real-ESRGAN）放大到 1944×1080：5/10 格"] and ctl.notes == []

    full_hd = make_clip(tmp_path / "full.mp4", "1920x1080")
    assert app.runners._upscale_videos(Ctl(), [full_hd], {}, "anime4k") == [full_hd]

    def broken(*args: object, **kwargs: object) -> None:
        raise AppError("Video2X 放大失敗（結束代碼 0x1）：boom", 500)

    monkeypatch.setattr(video2x, "upscale", broken)
    ctl, source = Ctl(), {}
    assert app.runners._upscale_videos(ctl, [clip], source, "anime4k") == [clip]
    assert "upscaler" not in source
    assert ctl.notes == ["Video2X 放大失敗，保留原本的影片：Video2X 放大失敗（結束代碼 0x1）：boom"]

    def canceled(*args: object, **kwargs: object) -> None:
        raise AppError("已取消", 499)

    monkeypatch.setattr(video2x, "upscale", canceled)
    with pytest.raises(AppError) as exc:
        app.runners._upscale_videos(Ctl(), [clip], {}, "anime4k")
    assert exc.value.status == 499


@needs_ffmpeg
@pytest.mark.skipif(not video2x.EXE.is_file(), reason="Video2X is not installed")
@pytest.mark.parametrize("method", ["anime4k", "realesrgan"])
def test_real_video2x_writes_a_1080p_h264_clip(app: App, tmp_path: Path, method: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    clip = make_clip(work / "clip.mp4", "192x108")  # small, so Real-ESRGAN takes seconds, not minutes
    dst = work / "clip-1080p.mp4"
    media = app.runners._media()
    video2x.upscale(media, clip, dst, 1920, 1080, method, cancelled=lambda: False)
    info = media.probe(dst)
    assert (info["width"], info["height"]) == (1920, 1080)
    assert sorted(p.name for p in work.iterdir()) == ["clip-1080p.mp4", "clip.mp4"]  # no x4 leftover


def add_video_take(app: App, pid: str, sid: str, clip: Path) -> str:
    """Stage `clip` as the shot's active video, the way a finished job would."""
    media = app.runners._media()
    tid = new_id("t")
    staged = stage_file(media, app.store.project_dir(pid), clip, TAKE_DIR, tid)
    commit_takes(app.store, pid, [new_take(sid, staged, take_id=tid, source={"backend": "comfy"}, prompt="清晨漁港")],
                 "activeVideo")
    return tid


@needs_ffmpeg
def test_upscalevideo_job_adds_a_1080p_take_of_the_chosen_video(app: App, client: Client, project: dict,
                                                                tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, sid = project["id"], project["shots"][0]["id"]
    with pytest.raises(AppError, match="還沒有影片"):
        app.runners.prepare("upscalevideo", pid, sid, {})
    small = add_video_take(app, pid, sid, make_clip(tmp_path / "clip.mp4"))
    monkeypatch.setattr(video2x, "EXE", tmp_path / "missing" / "video2x.exe")
    with pytest.raises(AppError, match="找不到 Video2X"):
        app.runners.prepare("upscalevideo", pid, sid, {})

    exe = tmp_path / "video2x.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(video2x, "EXE", exe)
    job = app.runners.prepare("upscalevideo", pid, sid, {"method": "realesrgan"})
    assert job["lane"] == "comfy" and job["payload"] == {"backend": "local", "source": small, "method": "realesrgan"}
    assert app.runners.prepare("upscalevideo", pid, sid, {"method": "waifu2x"})["payload"]["method"] == "anime4k"

    def scale_with_ffmpeg(media: object, src: Path, dst: Path, width: int, height: int, method: str,
                          cancelled: object, on_frames: object) -> None:
        subprocess.run([find_ffmpeg(), "-y", "-v", "error", "-i", str(src), "-vf", f"scale={width}:{height}",
                        "-pix_fmt", "yuv420p", str(dst)], check=True)

    monkeypatch.setattr(video2x, "upscale", scale_with_ffmpeg)
    body = {"kind": "upscalevideo", "pid": pid, "shotId": sid, "payload": {"source": small}}
    done = client.wait_job(client.json("POST", "/api/jobs", body)["job"]["id"], timeout=60)
    assert done["status"] == "done", done["error"]
    saved = app.store.get(pid)
    [big] = done["result"]["takeIds"]
    take = saved["takes"][big]
    assert saved["shots"][0]["activeVideo"] == big and take["parent"] == small
    assert (take["width"], take["height"]) == (1944, 1080) and take["label"] == "放大 1944×1080"
    assert take["source"]["upscaler"] == "Video2X Anime4K" and take["prompt"] == "清晨漁港"
    with pytest.raises(AppError, match="已經是 1080P 以上"):
        app.runners.prepare("upscalevideo", pid, sid, {})


def test_1080p_project_keeps_1920x1080_and_generates_at_720p(app: App, client: Client, project: dict) -> None:
    done = client.wait_job(submit_image(client, project)["id"], timeout=30)
    assert done["status"] == "done", done["error"]
    pid, sid = project["id"], project["shots"][0]["id"]
    app.store.apply(pid, [{"op": "project.update", "patch": {"settings": {"width": 1920, "height": 1080}}}])
    assert (app.store.get(pid)["settings"]["width"], app.store.get(pid)["settings"]["height"]) == (1920, 1080)
    job = app.runners.prepare("image", pid, sid, {"prompt": "清晨漁港"})
    assert (job["payload"]["width"], job["payload"]["height"]) == (1280, 720)
    assert "專案尺寸是 1920×1080，以 1280×720 生成，匯出時等比放大成專案尺寸" in job["notes"]
    # A 480P template caps lower still and says so once
    job = app.runners.prepare("video", pid, sid, {"workflow": "ltxv-2b-i2v"})
    assert min(job["payload"]["width"], job["payload"]["height"]) == 480
    assert not any(n.startswith("專案尺寸") for n in job["notes"])
