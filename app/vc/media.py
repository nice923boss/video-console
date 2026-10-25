"""ffmpeg discovery plus the small media operations the app needs.

Everything shells out to ffmpeg (no ffprobe needed: stream info is parsed from `ffmpeg -i`).
"""
from __future__ import annotations

import array
import collections
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .util import AppError

log = logging.getLogger("vc.media")

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
WEB_VIDEO_CODECS = {"h264", "vp8", "vp9", "av1"}
WEB_PIX_FMTS = {"yuv420p", "yuvj420p"}
# ffmpeg's own VP8/VP9 decoders drop the transparency a WebM carries beside the picture; libvpx keeps it.
ALPHA_DECODERS = {"vp8": "libvpx", "vp9": "libvpx-vp9"}
WAVE_RATE = 4000  # samples per second of the mono 16-bit audio the waveform envelope is computed from

QUALITY = {
    "high": {"crf": 18, "x264": "slow", "cq": 19, "nv": "p6", "abr": "192k"},
    "standard": {"crf": 21, "x264": "medium", "cq": 23, "nv": "p5", "abr": "160k"},
    "draft": {"crf": 26, "x264": "veryfast", "cq": 28, "nv": "p2", "abr": "128k"},
}

# Colour (QA-04): browsers and players read HD video without colour tags as BT.709, so the app does too.
# Video read into pictures is taken as BT.709 (AS_709); pictures written as video use the BT.709 matrix
# (TO_709) and are tagged to match (TAGS_709).
AS_709 = "setparams=colorspace=bt709"
TO_709 = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
TAGS_709 = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]

# Ken Burns presets: zoom, x, y expressions for zoompan; {e} is an eased 0..1 progress value.
KENBURNS = {
    "zoom_in": ("1+0.18*{e}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "zoom_out": ("1.18-0.18*{e}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "pan_left": ("1.15", "(iw-iw/zoom)*(1-{e})", "ih/2-(ih/zoom/2)"),
    "pan_right": ("1.15", "(iw-iw/zoom)*{e}", "ih/2-(ih/zoom/2)"),
    "pan_up": ("1.15", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*(1-{e})"),
    "pan_down": ("1.15", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*{e}"),
    "zoom_in_left": ("1+0.2*{e}", "(iw-iw/zoom)*0.15*(1-{e})", "ih/2-(ih/zoom/2)"),
    "zoom_in_right": ("1+0.2*{e}", "(iw-iw/zoom)*(0.85+0.15*{e})", "ih/2-(ih/zoom/2)"),
    "static": ("1", "0", "0"),
}
KENBURNS_LABELS = {
    "zoom_in": "緩慢推近",
    "zoom_out": "緩慢拉遠",
    "pan_left": "向左平移",
    "pan_right": "向右平移",
    "pan_up": "向上平移",
    "pan_down": "向下平移",
    "zoom_in_left": "推近並偏左",
    "zoom_in_right": "推近並偏右",
    "static": "靜止",
}


def find_ffmpeg(configured: str = "", tools_dir: Path | None = None) -> str | None:
    """Locate ffmpeg: configured path, PATH, the imageio-ffmpeg wheel, the copy the settings page installed into
    `tools_dir`, then common install spots."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    try:
        import imageio_ffmpeg  # type: ignore

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # noqa: BLE001 - optional dependency, any failure just means "not here"
        log.debug("imageio_ffmpeg unavailable: %s", exc)
    if tools_dir is not None:
        candidates.append(str(tools_dir / "ffmpeg" / "ffmpeg.exe"))
    local = os.environ.get("LOCALAPPDATA", "")
    candidates += [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        os.path.expanduser(r"~\scoop\shims\ffmpeg.exe"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def image_size(path: Path) -> tuple[int, int] | None:
    """Read width/height from PNG, JPEG, WebP, GIF or BMP headers without decoding."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                w, h = struct.unpack(">II", head[16:24])
                return w, h
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return w, h
            if head[:2] == b"BM":
                w, h = struct.unpack("<ii", head[18:26])
                return w, abs(h)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                data = head + f.read(16)
                chunk = data[12:16]
                if chunk == b"VP8 ":
                    w, h = struct.unpack("<HH", data[26:30])
                    return w & 0x3FFF, h & 0x3FFF
                if chunk == b"VP8L":
                    bits = int.from_bytes(data[21:25], "little")
                    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
                if chunk == b"VP8X":
                    return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
                return None
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        b = f.read(1)
                    if not b:
                        return None
                    marker = b[0]
                    if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                        continue
                    seg_len = struct.unpack(">H", f.read(2))[0]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(1)
                        h, w = struct.unpack(">HH", f.read(4))
                        return w, h
                    f.seek(seg_len - 2, 1)
    except (OSError, struct.error):
        return None
    return None


def parse_probe(text: str) -> dict:
    """Parse the stream summary ffmpeg prints for `-i file`."""
    info: dict = {
        "duration": None,
        "width": None,
        "height": None,
        "fps": None,
        "hasVideo": False,
        "hasAudio": False,
        "vcodec": None,
        "pixfmt": None,
        "acodec": None,
        "alpha": False,
    }
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 3)
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        if "Video:" in line and not info["hasVideo"] and "attached pic" not in line:
            info["hasVideo"] = True
            mv = re.search(r"Video: ([^\s,]+)[^,]*, ([a-z0-9_]+)", line)
            if mv:
                info["vcodec"], info["pixfmt"] = mv.group(1), mv.group(2)
            md = re.search(r", (\d{2,5})x(\d{2,5})", line)
            if md:
                info["width"], info["height"] = int(md.group(1)), int(md.group(2))
            mf = re.search(r", (\d+(?:\.\d+)?) fps", line) or re.search(r", (\d+(?:\.\d+)?) tbr", line)
            if mf:
                info["fps"] = float(mf.group(1))
        elif "Audio:" in line and not info["hasAudio"]:
            info["hasAudio"] = True
            ma = re.search(r"Audio: ([^\s,]+)", line)
            info["acodec"] = ma.group(1) if ma else None
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", text)
    if rot and info["width"] and abs(round(float(rot.group(1)))) % 180 == 90:
        info["width"], info["height"] = info["height"], info["width"]
    # A WebM tags a VP8/VP9 stream that carries a transparency plane with alpha_mode 1.
    info["alpha"] = info["vcodec"] in ALPHA_DECODERS and bool(re.search(r"^\s*alpha_mode\s*:\s*1\s*$", text, re.M))
    return info


def _envelope(pcm: bytes, log_text: str, buckets: int) -> tuple[list[int], float, float]:
    """(peaks 0..100, duration, level) from mono s16le audio at WAVE_RATE and ffmpeg's volumedetect log."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    n = len(samples)
    if n == 0:
        return [0] * 10, 0.0, 0.0
    per = max(1, -(-n // buckets))
    peaks = []
    for i in range(0, n, per):
        chunk = samples[i : i + per]
        peaks.append(max(max(chunk), -min(chunk)))
    top = max(peaks) or 1
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", log_text)
    level = 10 ** (float(m.group(1)) / 20) if m else top / 32768
    return [round(p * 100 / top) for p in peaks], round(n / WAVE_RATE, 3), round(level, 4)


class Media:
    def __init__(self, exe: str | None):
        self.exe = exe
        self._encoders: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._version: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def require(self) -> str:
        if not self.exe:
            raise AppError("找不到 ffmpeg：請到「設定 › 依賴元件」按「一鍵安裝」，或在「設定 › 影片輸出」指定 ffmpeg.exe", 503)
        return self.exe

    # ------------------------------------------------------------ process helpers
    def _base(self, loglevel: str = "error") -> list[str]:
        return [self.require(), "-hide_banner", "-nostdin", "-loglevel", loglevel, "-y"]

    def run(self, args: list[str], timeout: float = 600, cwd: str | Path | None = None) -> None:
        cmd = self._base() + args
        log.debug("ffmpeg %s", " ".join(args))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout, cwd=cwd, creationflags=NO_WINDOW
            )
        except subprocess.TimeoutExpired:
            raise AppError("ffmpeg 執行逾時", 500) from None
        if proc.returncode != 0:
            raise self._error(proc.stderr.decode("utf-8", "replace"))

    def run_progress(
        self,
        args: list[str],
        total: float,
        on_progress: Callable[[float], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        cwd: str | Path | None = None,
        timeout: float = 6 * 3600,
        stall: float = 180,
        loglevel: str = "error",
    ) -> str:
        """Run ffmpeg with `-progress pipe:1`, reporting 0..1 and honouring cancellation; returns the last
        lines ffmpeg logged (at `loglevel`, so "info" keeps a filter's closing report such as loudnorm's).

        A run whose output time stops advancing for `stall` seconds is killed (a stuck hardware
        encoder or filtergraph would otherwise block the queue forever).
        """
        cmd = self._base(loglevel) + ["-progress", "pipe:1", "-nostats"] + args
        log.debug("ffmpeg %s", " ".join(args))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            creationflags=NO_WINDOW,
        )
        tail: collections.deque[str] = collections.deque(maxlen=40)

        def pump_stderr() -> None:
            assert proc.stderr is not None
            for raw in proc.stderr:
                tail.append(raw.decode("utf-8", "replace").rstrip())

        reader = threading.Thread(target=pump_stderr, daemon=True)
        reader.start()
        deadline = time.time() + timeout
        killed = ""
        last_sec = -1.0
        last_advance = time.time()

        def watchdog() -> None:
            nonlocal killed
            while proc.poll() is None:
                if cancelled and cancelled():
                    killed = "cancel"
                elif time.time() > deadline:
                    killed = "timeout"
                elif time.time() - last_advance > stall:
                    killed = "stall"
                if killed:
                    proc.kill()
                    return
                time.sleep(0.25)

        threading.Thread(target=watchdog, daemon=True).start()
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("ascii", "replace").strip()
            # out_time_ms is (despite its name) also microseconds; N/A appears before the first frame.
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    sec = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if sec > last_sec:
                    last_sec, last_advance = sec, time.time()
                    if on_progress and total > 0:
                        on_progress(max(0.0, min(1.0, sec / total)))
        proc.wait()
        reader.join(timeout=2)
        if killed == "cancel":
            raise AppError("已取消", 499)
        if killed == "timeout":
            raise AppError("ffmpeg 執行逾時", 500)
        if killed == "stall":
            log.warning("ffmpeg stalled at %.1fs: %s", last_sec, "\n".join(list(tail)[-10:]))
            raise AppError(f"ffmpeg 超過 {int(stall)} 秒沒有進度，已中止", 500, detail="\n".join(tail))
        if proc.returncode != 0:
            raise self._error("\n".join(tail))
        if on_progress:
            on_progress(1.0)
        return "\n".join(tail)

    @staticmethod
    def _error(stderr: str) -> AppError:
        lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        last = lines[-1] if lines else "未知錯誤"
        log.warning("ffmpeg failed: %s", "\n".join(lines[-15:]))
        return AppError(f"ffmpeg 處理失敗：{last[:300]}", 500, detail="\n".join(lines[-15:]))

    # ------------------------------------------------------------ info
    def version(self) -> str:
        if self._version is None:
            if not self.exe:
                return ""
            try:
                out = subprocess.run(
                    [self.exe, "-hide_banner", "-version"], capture_output=True, timeout=20, creationflags=NO_WINDOW
                ).stdout.decode("utf-8", "replace")
                m = re.search(r"ffmpeg version (\S+)", out)
                self._version = m.group(1) if m else "unknown"
            except (OSError, subprocess.TimeoutExpired):
                self._version = "unknown"
        return self._version

    def major_version(self) -> int:
        m = re.match(r"n?(\d+)", self.version())
        return int(m.group(1)) if m else 0

    def filter_script_args(self, script: Path) -> list[str]:
        """Pass a long filtergraph through a file (keeps the command line short)."""
        if self.major_version() >= 7:
            return ["-/filter_complex", str(script)]
        return ["-filter_complex_script", str(script)]

    def probe(self, path: Path) -> dict:
        exe = self.require()
        try:
            proc = subprocess.run(
                [exe, "-hide_banner", "-nostdin", "-i", str(path)],
                capture_output=True,
                timeout=60,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise AppError("讀取媒體資訊逾時", 500) from None
        info = parse_probe(proc.stderr.decode("utf-8", "replace"))
        if not info["hasVideo"] and not info["hasAudio"]:
            raise AppError("無法讀取這個媒體檔（格式不支援或檔案損毀）", 415)
        return info

    def has_encoder(self, name: str) -> bool:
        with self._lock:
            if name in self._encoders:
                return self._encoders[name]
        ok = False
        if self.exe:
            try:
                proc = subprocess.run(
                    [self.exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                     "color=c=black:s=320x240:d=0.2", "-frames:v", "3", "-c:v", name, "-f", "null", "-"],
                    capture_output=True,
                    timeout=30,
                    creationflags=NO_WINDOW,
                )
                ok = proc.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        with self._lock:
            self._encoders[name] = ok
        log.info("Encoder %s %s", name, "available" if ok else "unavailable")
        return ok

    def pick_encoder(self, preference: str = "auto") -> str:
        if preference not in ("auto", "", None):
            if preference == "libx264" or self.has_encoder(preference):
                return preference
            log.warning("Encoder %s unavailable, falling back", preference)
        for name in ("h264_nvenc", "h264_amf", "h264_qsv"):
            if self.has_encoder(name):
                return name
        return "libx264"

    @staticmethod
    def encoder_args(encoder: str, quality: str = "high", maxrate: int = 0) -> list[str]:
        """maxrate (kbps) caps the video bitrate: x264 and NVENC keep their quality target under the cap;
        AMF and Quick Sync have no capped constant-quality mode here, so they switch to VBR at that rate."""
        q = QUALITY.get(quality, QUALITY["high"])
        cap = ["-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k"] if maxrate else []
        if encoder == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", q["nv"], "-tune", "hq", "-rc", "vbr", "-cq", str(q["cq"]),
                    "-b:v", "0", *cap, "-pix_fmt", "yuv420p", "-profile:v", "high"]
        if encoder == "h264_amf":
            rate = ["-rc", "vbr_peak", "-b:v", f"{maxrate}k", *cap] if maxrate else [
                "-rc", "cqp", "-qp_i", str(q["cq"]), "-qp_p", str(q["cq"] + 2)]
            return ["-c:v", "h264_amf", "-quality", "quality", *rate, "-pix_fmt", "yuv420p"]
        if encoder == "h264_qsv":
            rate = ["-b:v", f"{maxrate}k", *cap] if maxrate else ["-global_quality", str(q["cq"])]
            return ["-c:v", "h264_qsv", *rate, "-pix_fmt", "nv12"]
        return ["-c:v", "libx264", "-preset", q["x264"], "-crf", str(q["crf"]), *cap, "-pix_fmt", "yuv420p",
                "-profile:v", "high"]

    # ------------------------------------------------------------ operations
    def thumbnail(self, src: Path, dst: Path, width: int = 384, at: float | None = None) -> None:
        args: list[str] = []
        if at:
            args += ["-ss", f"{at:.3f}"]
        args += ["-i", str(src), "-frames:v", "1", "-an", "-sn",
                 "-vf", f"scale='min({width},iw)':-2:flags=lanczos", "-q:v", "4", str(dst)]
        self.run(args, timeout=120)

    def strip(self, src: Path, dst: Path, duration: float, count: int = 8) -> None:
        """Filmstrip sprite (count frames side by side) used for timeline thumbnails and scrubbing."""
        rate = max(0.01, count / max(0.1, duration))
        self.run(["-i", str(src), "-an", "-sn", "-vf",
                  f"fps={rate:.6f},scale=160:-2:flags=bilinear,tile={count}x1",
                  "-frames:v", "1", "-q:v", "5", str(dst)], timeout=180)

    def strip_and_waveform(self, src: Path, strip: Path, duration: float, count: int = 8,
                           buckets: int = 1000) -> tuple[list[int], float, float]:
        """strip() and waveform() of a video with sound as two outputs of one ffmpeg run, so the file is
        read once (GAPS #143). Returns what waveform() returns; the filmstrip is written to `strip`."""
        exe = self.require()
        rate = max(0.01, count / max(0.1, duration))
        try:
            proc = subprocess.run(
                [exe, "-hide_banner", "-nostdin", "-loglevel", "info", "-y", "-i", str(src),
                 "-map", "0:v:0", "-vf", f"fps={rate:.6f},scale=160:-2:flags=bilinear,tile={count}x1",
                 "-frames:v", "1", "-q:v", "5", str(strip),
                 "-map", "0:a:0", "-af", "volumedetect", "-ac", "1", "-ar", str(WAVE_RATE), "-f", "s16le", "-"],
                capture_output=True,
                timeout=300,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise AppError("分析影片縮圖與波形逾時", 500) from None
        if proc.returncode != 0 or not strip.is_file():
            raise self._error(proc.stderr.decode("utf-8", "replace"))
        return _envelope(proc.stdout, proc.stderr.decode("utf-8", "replace"), buckets)

    def waveform(self, src: Path, buckets: int = 1000) -> tuple[list[int], float, float]:
        """Peak envelope (0..100) for drawing audio clips; returns (peaks, duration, level). level is the
        file's loudest sample as a fraction of full scale, measured before the envelope is downmixed and
        resampled (which lowers peaks), so the timeline can mark where a clip's gain makes it clip."""
        exe = self.require()
        try:
            proc = subprocess.run(
                [exe, "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(src), "-vn", "-af", "volumedetect",
                 "-ac", "1", "-ar", str(WAVE_RATE), "-f", "s16le", "-"],
                capture_output=True,
                timeout=300,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise AppError("分析音訊波形逾時", 500) from None
        if proc.returncode != 0:
            raise self._error(proc.stderr.decode("utf-8", "replace"))
        return _envelope(proc.stdout, proc.stderr.decode("utf-8", "replace"), buckets)

    def pcm16(self, src: Path, rate: int = 16000) -> bytes:
        """The first audio stream as mono 16-bit little-endian samples at `rate` (chartimes.char_times)."""
        exe = self.require()
        try:
            proc = subprocess.run(
                [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1",
                 "-ar", str(rate), "-f", "s16le", "-"],
                capture_output=True,
                timeout=300,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise AppError("分析配音逐字時間逾時", 500) from None
        if proc.returncode != 0:
            raise self._error(proc.stderr.decode("utf-8", "replace"))
        return proc.stdout

    def last_frame(self, src: Path, dst: Path) -> None:
        self.run(["-sseof", "-0.5", "-i", str(src), "-an", "-vf", AS_709, "-update", "1", str(dst)], timeout=120)
        if not dst.exists():
            self.run(["-i", str(src), "-an", "-vf", AS_709, "-update", "1", str(dst)], timeout=300)

    def frame_at(self, src: Path, at: float, dst: Path) -> None:
        self.run(["-ss", f"{max(0.0, at):.3f}", "-i", str(src), "-frames:v", "1", "-an", "-vf", AS_709, str(dst)], timeout=120)

    def kenburns(
        self,
        src: Path,
        dst: Path,
        width: int,
        height: int,
        fps: int,
        duration: float,
        preset: str,
        cancelled: Callable[[], bool] | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> None:
        """Animate a still with a slow zoom/pan (local camera move, no AI model needed)."""
        z, x, y = KENBURNS.get(preset, KENBURNS["zoom_in"])
        frames = max(2, round(duration * fps))
        n1 = frames - 1
        ease = f"(on/{n1})*(on/{n1})*(3-2*(on/{n1}))"
        z, x, y = (part.replace("{e}", ease) for part in (z, x, y))
        sw, sh = width * 3, height * 3
        vf = (
            f"scale={sw}:{sh}:force_original_aspect_ratio=increase:flags=lanczos,crop={sw}:{sh},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},{TO_709}"
        )
        args = ["-i", str(src), "-vf", vf, "-frames:v", str(frames), "-an",
                *self.encoder_args("libx264", "high"), *TAGS_709, "-movflags", "+faststart", str(dst)]
        self.run_progress(args, frames / fps, on_progress, cancelled, timeout=1800)

    def reverse(
        self,
        src: Path,
        dst: Path,
        duration: float,
        has_audio: bool,
        cancelled: Callable[[], bool] | None = None,
        on_progress: Callable[[float], None] | None = None,
        piece: float = 5.0,
    ) -> None:
        """Play a video backwards (sound too). The reverse filter holds every frame in memory, so the video is
        reversed a few seconds at a time and the pieces are joined last to first."""
        work = dst.parent
        count = max(1, math.ceil(duration / piece - 1e-6))
        names: list[str] = []
        for i in range(count):
            if cancelled and cancelled():
                raise AppError("已取消", 499)
            name = f"rev-{i:04d}.mp4"
            args = ["-ss", f"{i * piece:.3f}", "-t", f"{piece:.3f}", "-i", str(src), "-vf", "reverse",
                    *self.encoder_args("libx264", "high")]
            args += ["-af", "areverse", "-c:a", "aac", "-b:a", "192k"] if has_audio else ["-an"]
            self.run([*args, str(work / name)], timeout=1800)
            names.insert(0, name)
            if on_progress:
                on_progress((i + 1) / (count + 1))
        (work / "rev-list.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
        self.run(["-f", "concat", "-safe", "0", "-i", "rev-list.txt", "-c", "copy", "-movflags", "+faststart",
                  dst.name], timeout=1800, cwd=work)

    def to_web_video(self, src: Path, dst: Path, cancelled: Callable[[], bool] | None = None) -> None:
        """Re-encode a video the browser cannot play (HEVC, ProRes, 10-bit...) into H.264 MP4."""
        info = self.probe(src)
        args = ["-i", str(src), *self.encoder_args("libx264", "standard"), "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        args += ["-c:a", "aac", "-b:a", "160k"] if info["hasAudio"] else ["-an"]
        args += ["-movflags", "+faststart", str(dst)]
        self.run_progress(args, info["duration"] or 1.0, None, cancelled, timeout=3 * 3600)

    @staticmethod
    def web_playable(info: dict, path: Path) -> bool:
        ext = path.suffix.lower()
        return (
            ext in (".mp4", ".m4v", ".webm")
            and (info.get("vcodec") or "") in WEB_VIDEO_CODECS
            and (info.get("pixfmt") or "yuv420p") in WEB_PIX_FMTS
        )
