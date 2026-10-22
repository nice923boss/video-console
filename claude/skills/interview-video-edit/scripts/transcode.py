"""Shrink camera originals (e.g. Sony XAVC MXF) to 1080p H.264 MP4 for Video Console.

Replaces HandBrake: picks the loudest audio track automatically (cameras often record
4 mono channels with only one live mic), uses NVENC when available, libx264 otherwise.

Usage:
    python transcode.py <input_dir> [--out <dir>] [--height 1080] [--force]
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

VIDEO_EXTS = (".mxf", ".mov", ".mp4", ".mts", ".m4v", ".avi", ".mkv")
MAX_BYTES = 4 * 1024 ** 3  # Video Console upload limit


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return json.loads(out)


def audio_levels(path, n_audio):
    """Mean volume (dB) of each audio track over a 60 s window."""
    levels = []
    for i in range(n_audio):
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", "20", "-t", "60", "-i", str(path),
             "-map", f"0:a:{i}", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
        levels.append(float(m.group(1)) if m and m.group(1) != "-inf" else -999.0)
    return levels


def has_nvenc():
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return "h264_nvenc" in r.stdout


def build_cmd(src, dst, audio_idx, height, nvenc):
    scale = f"scale_cuda=-2:{height}" if nvenc else f"scale=-2:{height}"
    cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "error", "-stats"]
    if nvenc:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    cmd += ["-i", str(src), "-map", "0:v:0"]
    if audio_idx is not None:
        cmd += ["-map", f"0:a:{audio_idx}"]
    cmd += ["-vf", scale]
    if nvenc:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                "-maxrate", "16M", "-bufsize", "32M", "-profile:v", "high"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    if audio_idx is not None:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


def verify(dst, src_duration):
    info = probe(dst)
    dur = float(info["format"]["duration"])
    size = dst.stat().st_size
    kinds = [s["codec_type"] for s in info["streams"]]
    problems = []
    if abs(dur - src_duration) > 1.0:
        problems.append(f"duration {dur:.1f}s vs source {src_duration:.1f}s")
    if size >= MAX_BYTES:
        problems.append(f"size {size / 1e9:.2f}GB >= 4GB")
    if "audio" not in kinds:
        problems.append("no audio stream")
    return dur, size, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_dir")
    ap.add_argument("--out", help="output dir (default: <input_dir>/1080p)")
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--force", action="store_true", help="re-encode even if output exists")
    args = ap.parse_args()

    src_dir = Path(args.input_dir)
    out_dir = Path(args.out) if args.out else src_dir / f"{args.height}p"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in src_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not files:
        sys.exit(f"找不到影片檔：{src_dir}")
    nvenc = has_nvenc()
    print(f"{len(files)} files, encoder={'h264_nvenc' if nvenc else 'libx264'} -> {out_dir}", flush=True)

    report = []
    for src in files:
        dst = out_dir / f"{src.stem}.mp4"
        info = probe(src)
        src_dur = float(info["format"]["duration"])
        if dst.exists() and not args.force:
            try:
                dur, size, problems = verify(dst, src_dur)
                if not problems:
                    print(f"[skip] {dst.name} already ok", flush=True)
                    report.append({"src": str(src), "out": str(dst), "duration": dur, "bytes": size, "skipped": True})
                    continue
            except (subprocess.CalledProcessError, KeyError, ValueError):
                pass
        n_audio = sum(1 for s in info["streams"] if s["codec_type"] == "audio")
        levels = audio_levels(src, n_audio) if n_audio > 1 else [0.0] * n_audio
        audio_idx = max(range(n_audio), key=lambda i: levels[i]) if n_audio else None
        print(f"[enc] {src.name} {src_dur:.0f}s audio levels={levels} -> track {audio_idx}", flush=True)
        r = subprocess.run(build_cmd(src, dst, audio_idx, args.height, nvenc))
        if r.returncode != 0 and nvenc:  # ffmpeg lists h264_nvenc even on machines without an NVIDIA GPU
            print(f"[retry] {src.name} h264_nvenc failed, using libx264", flush=True)
            r = subprocess.run(build_cmd(src, dst, audio_idx, args.height, False))
        if r.returncode != 0:
            print(f"[FAIL] {src.name} ffmpeg exit {r.returncode}", flush=True)
            report.append({"src": str(src), "out": str(dst), "error": f"ffmpeg exit {r.returncode}"})
            continue
        dur, size, problems = verify(dst, src_dur)
        status = "ok" if not problems else "WARN " + "; ".join(problems)
        print(f"[done] {dst.name} {dur:.1f}s {size / 1e6:.0f}MB {status}", flush=True)
        report.append({"src": str(src), "out": str(dst), "duration": dur, "bytes": size,
                       "audio_track": audio_idx, "audio_levels": levels, "problems": problems})

    (out_dir / "transcode_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report:", out_dir / "transcode_report.json", flush=True)


if __name__ == "__main__":
    main()
