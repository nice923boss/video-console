"""Gyro stabilization with Gyroflow (GPL-3.0, run as a separate program), the way Sony Catalyst Browse steadies
footage: the camera records its own rotation, so the shake is undone from that record instead of being guessed
from the picture. The app's copy of a camera file has lost the gyro data (an import keeps only the picture and
sound), so the original is looked up by name in the project's camera folder (settings.gyroFolder) and checked
against the app's copy (same frame rate, same length within a frame) before it is used.

Gyroflow 1.6.3 command-line facts this relies on, checked on one FX6 file (705_7558.MXF, 2.5 s at 29.97 fps):
- --export-metadata 3:<file> writes per-frame camera data; a file without gyro data gets identity org_quat.
- --preset with trim_ranges_ms renders only that range. 500 ms gave frame 15 as the first frame (a brightness
  trace of the trimmed and the full render correlated 0.95 at that offset), and 2 frames past the end came along,
  so the length is cut by the ffmpeg pass that follows.
- out-params ignores the bitrate (a 1080P render came out near 109 Mbps), so ffmpeg re-encodes the result; the
  sound comes from the app's own copy of the clip, so a stabilized copy sounds exactly like its footage.
- It keeps its settings, log and downloaded lens profiles under %LOCALAPPDATA%\\Gyroflow.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .media import NO_WINDOW
from .util import AppError

if TYPE_CHECKING:
    from .media import Media

log = logging.getLogger("vc.gyro")

EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Gyroflow" / "Gyroflow.exe"
PORTABLE_EXE: Path | None = None  # data/tools/gyroflow/Gyroflow.exe from the settings page; set by App
EXTS = (".mxf", ".mp4", ".mov")  # what camera originals with gyro data come as (Sony MXF / MP4, others MOV)
SEARCH_DEPTH = 3  # folder levels looked into below the camera folder (a card copy keeps XDROOT/Clip/)
_PROGRESS_RE = re.compile(r"Rendering progress: (\d+)/(\d+)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_gyro_cache: dict[tuple[str, int, int], bool] = {}  # (path, mtime, size) -> whether the file carries gyro data
_cache_lock = threading.Lock()


def find() -> Path | None:
    """An installed Gyroflow, else the portable copy in data/tools."""
    for exe in (EXE, PORTABLE_EXE):
        if exe is not None and exe.is_file():
            return exe
    return None


def require() -> Path:
    exe = find()
    if exe is None:
        raise AppError("找不到 Gyroflow：請到「設定 › 依賴元件」按「安裝」，或把防抖方式改回「畫面分析」", 503)
    return exe


def footage_name(item: dict) -> str:
    """The file name the footage was imported as, without its extension (the camera's clip name)."""
    name = (item.get("source") or {}).get("name") or item.get("name") or item.get("label") or ""
    return Path(str(name)).stem


def candidates(folder: Path, stem: str) -> list[Path]:
    """Files named stem.mxf / .mp4 / .mov (any case) in folder and up to SEARCH_DEPTH levels below it."""
    want = stem.casefold()
    found: list[Path] = []
    base = len(folder.parts)
    for root, dirs, files in os.walk(folder):
        if len(Path(root).parts) - base >= SEARCH_DEPTH:
            dirs.clear()
        found += [Path(root) / f for f in files
                  if Path(f).stem.casefold() == want and Path(f).suffix.lower() in EXTS]
    return sorted(found)


def mismatch(info: dict, duration: float, fps: float) -> str | None:
    """Why a probed original cannot stand in for footage of this length and frame rate (None: it can)."""
    theirs = float(info.get("fps") or 0.0)
    if fps and theirs and abs(theirs - fps) > 0.01:
        return f"影格率 {theirs:g} 和素材的 {fps:g} 不同"
    length = float(info.get("duration") or 0.0)
    if abs(length - duration) > 1.0 / (fps or theirs or 30.0) + 0.02:
        return f"長度 {length:.2f} 秒和素材的 {duration:.2f} 秒不同"
    return None


def has_gyro(path: Path) -> bool:
    """Whether Gyroflow reads camera rotation from the file (remembered while the file stays the same)."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if key in _gyro_cache:
            return _gyro_cache[key]
    with tempfile.TemporaryDirectory(prefix="vc-gyro-") as tmp:
        out = Path(tmp) / "camera.json"
        run([str(require()), str(path), "--export-metadata", f"3:{out.as_posix()}"], lambda: False, timeout=600)
        try:
            frames = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AppError(f"Gyroflow 沒有讀出 {path.name} 的鏡頭資料：{exc}", 500) from None
    moved = any(abs(q[0] - 1.0) > 1e-6 or any(abs(v) > 1e-6 for v in q[1:])
                for f in frames if isinstance(f, dict)
                for q in [f.get("org_quat") or [1.0, 0.0, 0.0, 0.0]])
    with _cache_lock:
        _gyro_cache[key] = moved
    return moved


def locate(media: Media, folder: str, item: dict) -> Path:
    """The camera original of a video item (a take or a library item): same name in `folder`, same frame rate,
    same length within a frame, with gyro data. Raises a message saying what to fix."""
    if not folder.strip():
        raise AppError("陀螺儀防抖要用相機原始檔：請在防抖設定填「相機原始檔資料夾」（記憶卡複製出來的資料夾）")
    root = Path(folder.strip())
    if not root.is_dir():
        raise AppError(f"找不到相機原始檔資料夾：{root}", 404)
    stem = footage_name(item)
    found = candidates(root, stem) if stem else []
    if not found:
        raise AppError(f"在「{root}」找不到「{stem or '這段影片'}」的相機原始檔（.MXF、.MP4、.MOV，同檔名）", 404)
    duration, fps = float(item.get("duration") or 0.0), float(item.get("fps") or 0.0)
    reasons = []
    for path in found:
        why = mismatch(media.probe(path), duration, fps)
        if why is None and not has_gyro(path):
            why = "沒有陀螺儀資料"
        if why is None:
            return path
        reasons.append(f"{path.relative_to(root)}（{why}）")
    raise AppError("相機原始檔不能用：" + "；".join(reasons), 422)


def output_size(width: int, height: int, frame_w: int, frame_h: int) -> tuple[int, int]:
    """The original's shape, scaled down to fit the project frame (never up), both sides even."""
    if width <= 0 or height <= 0:
        return 2 * (frame_w // 2), 2 * (frame_h // 2)
    scale = min(1.0, frame_w / width, frame_h / height)
    return max(2, 2 * round(width * scale / 2)), max(2, 2 * round(height * scale / 2))


def render(src: Path, dst: Path, start: float, end: float, fps: float, size: tuple[int, int], smoothness: float,
           cancelled: Callable[[], bool], on_frames: Callable[[int, int], None] | None = None) -> float:
    """Gyroflow steadies [start, end) of the camera original into dst (H.264, no sound, size), plus up to a few
    frames after end. Returns the time of the first frame it wrote: the frame at or just before `start`, so the
    copy still covers everything from `start` on."""
    first = math.floor(start * fps + 1e-6)
    # Asking for 0.3 frame before the wanted frame lands on it whether Gyroflow rounds the time or takes the next
    # frame at or after it (500 ms at 29.97 fps, 14.99 frames, gave frame 15).
    begin_ms = max(0.0, (first - 0.3) * 1000.0 / fps)
    end_ms = (end + 1.0 / fps) * 1000.0
    params = {"codec": "H.264/AVC", "use_gpu": True, "audio": False, "output_width": size[0],
              "output_height": size[1], "output_folder": dst.parent.as_posix() + "/", "output_filename": dst.name}
    preset = {"version": 2, "trim_ranges_ms": [[round(begin_ms, 3), round(end_ms, 3)]],
              "stabilization": {"method": "Default",
                                "smoothing_params": [{"name": "smoothness", "value": smoothness}]}}
    run([str(require()), str(src), "-f", "--stdout-progress", "-p", json.dumps(params), "--preset",
         json.dumps(preset)], cancelled, on_frames)
    if not dst.is_file() or dst.stat().st_size == 0:
        raise AppError("Gyroflow 沒有產生影片", 500)
    return first / fps


def finish(media: Media, steady: Path, footage: Path, dst: Path, start: float, length: float, has_audio: bool,
           cancelled: Callable[[], bool] | None = None, on_progress: Callable[[float], None] | None = None) -> None:
    """The copy the app keeps: Gyroflow's picture cut to `length`, re-encoded at the app's quality, with the
    sound of the app's own footage from `start`."""
    args = ["-i", str(steady)]
    if has_audio:
        args += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(footage), "-map", "0:v:0", "-map", "1:a:0",
                 "-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-map", "0:v:0", "-an"]
    args += ["-t", f"{length:.3f}", *media.encoder_args("libx264", "high"), "-movflags", "+faststart", str(dst)]
    media.run_progress(args, length, on_progress, cancelled)


def run(cmd: list[str], cancelled: Callable[[], bool], on_frames: Callable[[int, int], None] | None = None,
        timeout: float = 3 * 3600) -> None:
    """Run Gyroflow, reporting its "Rendering progress: N/M frames" lines; a non-zero exit is an error."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW)
    tail: collections.deque[str] = collections.deque(maxlen=40)

    def pump() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = _ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
            match = _PROGRESS_RE.search(line)
            if match:
                if on_frames:
                    on_frames(int(match[1]), int(match[2]))
            elif line:
                tail.append(line)

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
                raise AppError("已取消", 499) if cancelled() else AppError("Gyroflow 執行逾時", 500)
    reader.join(timeout=5)
    if proc.returncode == 0:
        return
    code = f"{proc.returncode & 0xFFFFFFFF:#x}"
    log.warning("Gyroflow failed (%s): %s", code, "\n".join(list(tail)[-15:]))
    errors = [line for line in tail if "[ERROR]" in line or "error" in line.lower()]
    last = (errors or list(tail) or ["沒有錯誤訊息"])[-1][:300]
    raise AppError(f"Gyroflow 執行失敗（結束代碼 {code}）：{last}", 500)
