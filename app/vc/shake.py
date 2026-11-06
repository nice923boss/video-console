"""How much a video shot shakes, and which shots to stabilize and how (behind the auto-stabilize endpoint).

Shake is read with libvidstab's own motion search (vidstabdetect, written as an ascii motion file) on the shot's used
range scaled to WIDTH pixels: per frame the local motion fields are averaged after dropping the outer quarters (so a
person walking through counts less than the camera), summed into the camera path, and compared with that path
averaged over about half a second each side. The RMS of the difference, in percent of the picture width, is the
jitter: handheld wobble, not a pan or a push in. On the 4K event footage this was tuned on, tripod shots read
0.03-0.2 %, careful handheld 0.3-0.5 % and walking shots 0.6-1.2 %.

A shot at or past the threshold is stabilized with the camera path smoothed over about a second each side
(smoothing = round(fps)): in trials that took a 1.19 % shot to 0.16 %, where the default 15 frames left 0.57 %.
A stabilized shot is measured on the footage it was made from, and its copy is measured too (`residual`).
body.engine "gyro" or "auto" makes the copies with Gyroflow from the camera originals (gyro.py) instead; the shake
is still measured from the picture, so the same threshold decides which shots get stabilized.
"""
from __future__ import annotations

import math
import re
import statistics
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import edits, gyro
from .autocolor import number, shot_source, source_file
from .media import Media, clean_stab
from .sequence import build_sequence, video_take
from .util import AppError

WIDTH = 640  # analysis width; jitter is in percent of it, so any width reads the same
THRESHOLD = 0.3  # default jitter (percent of the picture width) from which a shot is stabilized
SMOOTH_SECONDS = 1.0  # camera path smoothing each side, in seconds of the shot's frame rate
MIN_FRAMES = 8  # fewer measured frames than this is too short to tell shake from movement
LM = re.compile(r"\(LM (-?\d+) (-?\d+) ")


def motions(media: Media, path: Path, start: float, length: float) -> list[tuple[float, float]]:
    """Per frame (dx, dy) camera movement in pixels at WIDTH, from vidstabdetect's local motion fields."""
    detect = f"scale={WIDTH}:-2,vidstabdetect=shakiness=5:accuracy=15:fileformat=ascii:result=m.trf"
    with tempfile.TemporaryDirectory(prefix="vc-shake-") as tmp:
        media.run(["-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.05, length):.3f}", "-i", str(path), "-vf", detect,
                   "-an", "-sn", "-f", "null", "-"], timeout=900, cwd=tmp)
        text = (Path(tmp) / "m.trf").read_text("latin-1")
    out: list[tuple[float, float]] = []
    for line in text.splitlines():
        if not line.startswith("Frame"):
            continue
        fields = [(int(m[1]), int(m[2])) for m in LM.finditer(line)]
        if len(fields) < 4:
            out.append((0.0, 0.0))
            continue
        xs, ys = sorted(f[0] for f in fields), sorted(f[1] for f in fields)
        q = len(xs) // 4
        out.append((statistics.fmean(xs[q:len(xs) - q]), statistics.fmean(ys[q:len(ys) - q])))
    return out


def jitter(mv: list[tuple[float, float]], fps: float) -> dict:
    """{jitter, speed} in percent of the picture width: the RMS of the camera path off its ±0.5 s average (a
    steady pan or push reads 0), and the average movement per frame (high for a pan, low for a held shot)."""
    n = len(mv)
    if not n:
        return {"jitter": 0.0, "speed": 0.0}
    k = max(2, round(fps * 0.5))
    px, py, x, y = [], [], 0.0, 0.0
    for dx, dy in mv:
        x, y = x + dx, y + dy
        px.append(x)
        py.append(y)

    def smooth(p: list[float]) -> list[float]:
        # the window shrinks evenly at the ends: a lopsided one would read a steady pan as shake there
        return [statistics.fmean(p[i - m:i + m + 1]) for i in range(n) for m in [min(k, i, n - 1 - i)]]

    sx, sy = smooth(px), smooth(py)
    rms = math.sqrt(sum((px[i] - sx[i]) ** 2 + (py[i] - sy[i]) ** 2 for i in range(n)) / n)
    speed = statistics.fmean(math.hypot(dx, dy) for dx, dy in mv)
    return {"jitter": round(100 * rms / WIDTH, 3), "speed": round(100 * speed / WIDTH, 3)}


def settings_for(fps: float, extra: dict | None = None) -> dict:
    """The stabilize settings for a shaky shot at this frame rate, with `extra` (STAB_DEFAULTS keys) on top."""
    return clean_stab({"smoothing": max(1, round(fps * SMOOTH_SECONDS)), **(extra or {})})


def measure(media: Media, project_dir: Path, project: dict, shot_id: str) -> dict:
    """Jitter of a video shot's used range (on the original footage when it is stabilized, plus `residual`, the
    stabilized copy's own jitter)."""
    shot, take, t_in, speed, length = shot_source(project, shot_id)
    if take.get("kind") != "video":
        raise AppError("防抖只能用在影片鏡頭")
    span = max(0.05, length * speed)
    out: dict = {"id": shot["id"], "name": shot.get("name") or "",
                 "stabilized": (take.get("source") or {}).get("tool") == "stabilize"}
    source, at = take, t_in
    if out["stabilized"]:
        source = (project.get("takes") or {}).get(take.get("parent"))
        if not source:
            raise AppError("找不到防抖前的原始影片")
        at = float(take["source"].get("start") or 0.0) + t_in
    mv = motions(media, source_file(project_dir, source), at, span)
    fps = len(mv) / span if len(mv) >= MIN_FRAMES else float(source.get("fps") or 30)
    out.update({**jitter(mv, fps), "frames": len(mv), "fps": round(fps, 3)})
    if out["stabilized"]:
        out["residual"] = jitter(motions(media, source_file(project_dir, take), t_in, span), fps)["jitter"]
        out["settings"] = clean_stab(take["source"].get("settings"))
    return out


ENGINES = ("vidstab", "gyro", "auto")  # auto: gyro where the camera original is found, else vidstab


def _gyro_original(media: Media, project: dict, folder: str, shot_id: str) -> str:
    """Path of the camera original of a shot's footage (the parent of a stabilized copy); raises why not."""
    take = video_take(project, next(s for s in project["shots"] if s["id"] == shot_id))
    if (take.get("source") or {}).get("tool") == "stabilize":
        take = (project.get("takes") or {}).get(take.get("parent")) or take
    return str(gyro.locate(media, folder, take))


def auto_stabilize(media: Media, project_dir: Path, project: dict, body: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Measure the video shots in body.ids (default: every video shot in the sequence) and stabilize those at or past
    body.threshold, by body.engine (ENGINES; body.gyroFolder also sets the project's camera folder). Returns
    (shot ops, stabilize jobs to queue, per-shot report)."""
    if not media.has_filter("vidstabdetect") or not media.has_filter("vidstabtransform"):
        raise AppError("這個 ffmpeg 沒有防抖濾鏡（libvidstab）：請到「設定 › 影片輸出」指定含 libvidstab 的 ffmpeg.exe", 501)
    threshold = min(5.0, max(0.05, number(body.get("threshold") or THRESHOLD, "threshold")))
    extra = body.get("settings")
    if extra is not None and not isinstance(extra, dict):
        raise AppError("防抖設定 settings 要是物件")
    clean_stab(extra)  # a typo in settings is an error before anything is measured
    engine = str(body.get("engine") or "vidstab")
    if engine not in ENGINES:
        raise AppError(f"engine 只能是 {'、'.join(ENGINES)}")
    saved = str((project.get("settings") or {}).get("gyroFolder") or "")
    folder = str(body["gyroFolder"]).strip() if body.get("gyroFolder") is not None else saved
    if engine == "gyro":
        gyro.require()
    if engine == "auto" and gyro.find() is None:
        engine, fallback = "vidstab", "（沒有安裝 Gyroflow，用畫面分析）"
    else:
        fallback = ""
    ids = body.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise AppError("ids 必須是鏡頭 id 清單")
    clips = [c for c in build_sequence(project, include_all=bool(ids))
             if c["source"] != "empty" and c["take"].get("kind") == "video"]
    if ids:
        wanted = {str(i) for i in ids}
        clips = [c for c in clips if c["shot"]["id"] in wanted]
        missing = wanted - {c["shot"]["id"] for c in clips}
        if missing:
            raise AppError(f"找不到影片鏡頭：{'、'.join(sorted(missing))}", 404)
    if not clips:
        raise AppError("沒有影片鏡頭可以防抖")
    with ThreadPoolExecutor(max_workers=3) as pool:
        readings = list(pool.map(lambda c: measure(media, project_dir, project, c["shot"]["id"]), clips))

    ops: list[dict] = []
    if folder != saved:
        ops.append({"op": "project.update", "patch": {"settings": {"gyroFolder": folder}}})
    jobs: list[dict] = []
    report: list[dict] = []
    for r in readings:
        row = {**r, "threshold": threshold}
        if r["frames"] < MIN_FRAMES:
            row.update(decision="short", reason=f"只有 {r['frames']} 格，太短，無法判斷晃動")
        elif r["jitter"] < threshold:
            kept = "，保留現有防抖" if r["stabilized"] else ""
            row.update(decision="steady", reason=f"晃動 {r['jitter']:.2f}%，低於門檻 {threshold:.2f}%{kept}")
        else:
            want, note = settings_for(r["fps"], extra), fallback
            if engine != "vidstab":
                try:
                    row["original"] = _gyro_original(media, project, folder, r["id"])
                    want = clean_stab({**(extra or {}), "engine": "gyro"})
                except AppError as exc:
                    if engine == "gyro":
                        report.append({**row, "decision": "nogyro", "reason": exc.message})
                        continue
                    note = f"（{exc.message}，改用畫面分析）"
            row["settings"] = want
            how = (f"陀螺儀防抖，平滑度 {want['gyroSmooth']:g}" if want["engine"] == "gyro"
                   else f"防抖，平滑前後各 {want['smoothing']} 格")
            if r["stabilized"] and r.get("settings") == want:
                row.update(decision="done", reason=f"晃動 {r['jitter']:.2f}%，已用這組設定防抖"
                                                   f"（剩 {r['residual']:.2f}%）{note}")
            else:
                plan = edits.stabilize(project, r["id"], on=True, settings=want)
                ops += plan["ops"]
                if plan.get("job"):
                    jobs.append(plan["job"])
                again = ("" if not r["stabilized"] else "，改用陀螺儀重做" if want["engine"] == "gyro"
                         and r["settings"]["engine"] != "gyro" else "，重做防抖")
                row.update(decision="stabilize", job=bool(plan.get("job")),
                           reason=f"晃動 {r['jitter']:.2f}%，達門檻 {threshold:.2f}%：{how}{again}{note}")
        report.append(row)
    return ops, jobs, report
