"""Video2X (a local GPU upscaler, installed separately) for clips made at 480P: when the user turns it on, a
ComfyUI video is brought up to 1080P before it becomes a take, so the timeline gets the upscaled clip.

Two methods, timed on a 5-second 864x480 LTX clip with an RTX 4050 Laptop:
- anime4k: libplacebo with the Anime4K v4 A shader writes the target size directly in about 4 s. Edges get
  sharper, but fine realistic texture is smoothed away.
- realesrgan: Real-ESRGAN plus only scales x4 (3456x1920 here), so ffmpeg scales that down to the target. It
  keeps more detail and took 321 s plus 5 s for the downscale.
Video2X 6.4 drops the last two frames of every clip. Its Real-ESRGAN processor also crashes (0xC0000005) while the
process exits, after the output file is complete; the "Video processed successfully" log line tells that crash
apart from a real failure.
"""
from __future__ import annotations

import collections
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .media import NO_WINDOW
from .util import AppError

if TYPE_CHECKING:
    from .media import Media

log = logging.getLogger("vc.video2x")

EXE = Path(r"C:\Program Files\Video2X Qt6\video2x.exe")
PORTABLE_EXE: Path | None = None  # data/tools/video2x/video2x.exe from the settings page; set by App
SHADER = "anime4k-v4-a"
ESRGAN_MODEL = "realesrgan-plus"
METHODS = {"anime4k": "Anime4K", "realesrgan": "Real-ESRGAN"}  # setting value -> name shown in notes
# Part of a generate-then-upscale job's progress bar the upscale gets (PF-06, GAPS #81), from the timings above:
# Anime4K takes seconds; Real-ESRGAN takes minutes, about as long as a ComfyUI video, so half the bar is a guess.
PROGRESS_SHARE = {"anime4k": 0.05, "realesrgan": 0.5}
SHORT_SIDE = 1080
DONE_LINE = "Video processed successfully"
_FRAME_RE = re.compile(r"frame=(\d+)/(\d+)")


def target_size(width: int, height: int) -> tuple[int, int] | None:
    """The same shape with the short side at 1080 (both sides even), or None when the clip is already that big."""
    short = min(width, height)
    if short <= 0 or short >= SHORT_SIDE:
        return None
    scale = SHORT_SIDE / short
    return 2 * round(width * scale / 2), 2 * round(height * scale / 2)


def find() -> Path | None:
    """The installed Video2X Qt6, else the portable copy in data/tools."""
    for exe in (EXE, PORTABLE_EXE):
        if exe is not None and exe.is_file():
            return exe
    return None


def require() -> Path:
    exe = find()
    if exe is None:
        raise AppError("找不到 Video2X：請到「設定 › 依賴元件」按「安裝」，或取消勾選「生影片後用 Video2X 放大到 1080P」", 503)
    return exe


def command(src: Path, dst: Path, width: int, height: int, method: str = "anime4k") -> list[str]:
    """Anime4K writes width x height; Real-ESRGAN writes 4x at a high quality for the caller to scale down.

    The info log level is what prints DONE_LINE; the progress bar ("frame=N/T") goes to stdout as well.
    """
    if method == "realesrgan":
        upscaler = ["-p", "realesrgan", "-s", "4", "--realesrgan-model", ESRGAN_MODEL]
        quality = ["-e", "crf=12", "-e", "preset=veryfast"]
    else:
        upscaler = ["-p", "libplacebo", "-w", str(width), "-h", str(height), "--libplacebo-shader", SHADER]
        quality = ["-e", "crf=18", "-e", "preset=medium"]
    return [str(require()), "-i", str(src), "-o", str(dst), *upscaler, "-c", "libx264", *quality,
            "--pix-fmt", "yuv420p", "--log-level", "info"]


def upscale(media: Media, src: Path, dst: Path, width: int, height: int, method: str,
            cancelled: Callable[[], bool], on_frames: Callable[[int, int], None] | None = None) -> None:
    """Write `src` upscaled to width x height into `dst`; the caller probes `dst` afterwards."""
    if method != "realesrgan":
        run(command(src, dst, width, height, method), cancelled, on_frames)
        return
    big = dst.with_name(f"{dst.stem}-x4.mp4")
    try:
        run(command(src, big, width, height, method), cancelled, on_frames)
        media.run_progress(["-i", str(big), "-vf", f"scale={width}:{height}:flags=lanczos", "-an",
                            *media.encoder_args("libx264", "high"), "-movflags", "+faststart", str(dst)],
                           1.0, None, cancelled, timeout=1800)
    finally:
        big.unlink(missing_ok=True)


def run(cmd: list[str], cancelled: Callable[[], bool], on_frames: Callable[[int, int], None] | None = None,
        timeout: float = 3600) -> None:
    """Run Video2X, reporting its progress bar; a crash after DONE_LINE still counts as success."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW)
    tail: collections.deque[str] = collections.deque(maxlen=40)
    finished = threading.Event()

    def read_line(raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").replace("\x1b[K", "").strip()
        match = _FRAME_RE.match(line)
        if match:
            if on_frames:
                on_frames(int(match[1]), int(match[2]))
        elif line:
            tail.append(line)
            if DONE_LINE in line:
                finished.set()

    def pump() -> None:
        assert proc.stdout is not None
        rest = b""
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):  # the progress bar redraws with \r, not \n
            *lines, rest = re.split(rb"[\r\n]+", rest + chunk)
            for raw in lines:
                read_line(raw)
        read_line(rest)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.time() + timeout
    while True:
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancelled() or time.time() > deadline:
                proc.kill()
                proc.wait()
                raise AppError("已取消", 499) if cancelled() else AppError("Video2X 執行逾時", 500)
    reader.join(timeout=5)
    if proc.returncode == 0:
        return
    code = f"{proc.returncode & 0xFFFFFFFF:#x}"
    if finished.is_set():
        log.info("Video2X exited with %s after finishing the video; keeping its output", code)
        return
    log.warning("Video2X failed (%s): %s", code, "\n".join(list(tail)[-15:]))
    last = tail[-1][:300] if tail else "沒有錯誤訊息"
    raise AppError(f"Video2X 放大失敗（結束代碼 {code}）：{last}", 500)
