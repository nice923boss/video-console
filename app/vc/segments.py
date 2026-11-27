"""分段平行輸出: the timeline cut into segments where a run of clips starts, each segment rendered by its own ffmpeg from
only the inputs it shows, the sound mixed once into a WAV beside them, and the segments joined without re-encoding.

One graph opened every input of the project when it started (539 on a 15-minute course video) and ran every filter
on one timeline. A segment opens a fraction of them, and several run side by side. MPEG-TS segments join frame for
frame with -c copy; MP4 segments left one short frame at each seam (docs/spike-EA.md, S2a).

Each rendered segment is kept in media/cache/segments/ under a key of everything that makes its pictures
(segment_key), so exporting again after an edit renders only the segments the edit touched. The mix is kept there
too, under the key of the sound-only plan, and mixed again only when the sound changed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import Callable

from .export import (audio_bitrate, clip_runs, loudnorm_filter, measure_loudness, plan_audio_export, plan_export,
                     video_codec_args)
from .framegrid import clip_frames, frame_at, frame_rate, frames_per_second
from . import BUILD
from .media import Media
from .sequence import total_duration
from .util import AppError

log = logging.getLogger(__name__)

MIN_TOTAL = 120.0  # seconds: a shorter timeline renders as one graph
MIN_SEGMENT = 30.0  # seconds: no segment is shorter
AUDIO_SHARE, JOIN_SHARE = 0.05, 0.03  # of the progress bar; the pictures take the rest, by frame count
STORE = "segments"  # under media/cache: the rendered segments kept for the next export


class JoinFailed(Exception):
    """The segments rendered but would not join; the export starts again as one graph."""


def workers() -> int:
    """ffmpeg processes run side by side: each already keeps a few cores busy."""
    return max(2, min(4, (os.cpu_count() or 8) // 4))


def cut_frames(project: dict, clips: list[dict], count: int) -> list[int]:
    """The first frame of each segment after the first: for each even split of the timeline into `count`, the run
    start (a cut, never inside a transition) that costs least. Moving a cut a second from its even split costs 0.2; a
    video overlay running across a cut costs 0.1 per second of it before the cut, which the later segment decodes
    only to drop. Every segment keeps MIN_SEGMENT seconds."""
    fps = float(project["settings"]["fps"])
    frames = clip_frames(clips, total_duration(clips), fps)
    rate, end = frames_per_second(fps), frames[-1][1]
    starts = [frames[run[0]][0] for run in clip_runs(clips)][1:]
    all_media = project.get("media") or {}
    videos = []
    for item in project.get("overlays") or []:
        if (all_media.get(item.get("mediaId") or "") or {}).get("kind") == "video" and not item.get("disabled"):
            start = float(item.get("start") or 0.0)
            videos.append((frame_at(start, fps), frame_at(start + float(item.get("length") or 0.0), fps)))
    gap = MIN_SEGMENT * rate

    def cost(cut: int, ideal: float) -> float:
        waste = sum((cut - a) / rate for a, b in videos if a < cut < b)
        return 0.2 * abs(cut - ideal) / rate + 0.1 * waste

    cuts: list[int] = []
    for k in range(1, count):
        options = [c for c in starts if c - (cuts[-1] if cuts else 0) >= gap and end - c >= gap]
        if options:
            cuts.append(min(options, key=lambda c: cost(c, end * k / count)))
    return cuts


def plan_segments(project: dict, project_dir: Path, clips: list[dict], opts: dict, ass_rel: str | None,
                  luts: list[tuple[float, float, str]], shot_luts: dict[int, str]) -> list[dict] | None:
    """One plan_export per segment, or None when the export runs as one graph: turned off, a timeline under
    MIN_TOTAL, or no cut to split it at."""
    total = total_duration(clips)
    if not opts.get("segmented") or total < MIN_TOTAL:
        return None
    count = min(2 * workers(), int(total // MIN_SEGMENT))
    cuts = cut_frames(project, clips, count)
    if not cuts:
        return None
    end = clip_frames(clips, total, float(project["settings"]["fps"]))[-1][1]
    bounds = [0, *cuts, end]
    plans = []
    for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
        # Each segment burns its own ASS script (write_segment_ass): only the events it shows.
        rel = ass_rel and f"{ass_rel.removesuffix('.ass')}-seg{k:02d}.ass"
        plans.append({**plan_export(project, project_dir, clips, opts, rel, luts, shot_luts, window=(a, b)),
                      "ass": rel})
    return plans


def seam_times(plans: list[dict]) -> list[float]:
    """The timeline time of each seam between segments."""
    return [p["window"][0] / frames_per_second(p["fps"]) for p in plans[1:]]


def join_listing(files: list[Path], counts: list[int], fps: float, base: Path) -> str:
    """The concat demuxer's listing of the segments, each with its exact length. The demuxer starts a file where
    the one before ends, and without a length it takes the file's estimated duration: on a 3,522-frame segment of a
    real export that estimate was the span of its timestamps, a frame short, and the next segment came a frame early
    (a doubled timestamp at the seam, the pictures a frame ahead of the sound after it). Lengths are whole
    microseconds, the demuxer's unit, taken from the running frame count, so every file starts on its own frame."""
    rate = Fraction(frame_rate(fps))
    lines, start, frames = [], 0, 0
    for f, n in zip(files, counts):
        frames += n
        end = round(frames * 1_000_000 / rate)
        length = end - start
        lines.append(f"file '{f.relative_to(base).as_posix()}'\n"
                     f"duration {length // 1_000_000}.{length % 1_000_000:06d}\n")
        start = end
    return "".join(lines)


def _ass_seconds(stamp: str) -> float:
    h, m, s = stamp.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def segment_ass(ass: str, start: float, end: float) -> str:
    """The ASS script with only the events showing between start and end (seconds; a second's margin each side),
    in their order: a caption edited elsewhere leaves this script, and so the segment's key, as it was. Every event
    showing on one of the segment's frames stays, so libass places them as in the whole script."""
    lines = []
    for line in ass.splitlines(keepends=True):
        if line.startswith("Dialogue:"):
            _, on, off, _ = line.split(",", 3)
            if _ass_seconds(off) < start - 1.0 or _ass_seconds(on) > end + 1.0:
                continue
        lines.append(line)
    return "".join(lines)


def write_segment_ass(project_dir: Path, plans: list[dict], ass: str | None) -> list[Path]:
    """Write each segment's ASS script (plan_segments named them); returns the files written."""
    written = []
    for plan in plans:
        if plan.get("ass") and ass:
            rate = frames_per_second(plan["fps"])
            path = project_dir / plan["ass"]
            path.write_text(segment_ass(ass, plan["window"][0] / rate, plan["window"][1] / rate), encoding="utf-8")
            written.append(path)
    return written


def segment_key(media: Media, plan: dict, codec: list[str], project_dir: Path, stem: str) -> str:
    """What makes a segment's pictures, hashed: the app build and ffmpeg version, its window, inputs and graph,
    each input file's size and modification time, the encoder options, and the content of the export's own files
    the graph names (its ASS script and LUTs, whose names carry the export's time stamp `stem`)."""
    def content(m: re.Match) -> str:
        return hashlib.sha1((project_dir / m.group(0)).read_bytes()).hexdigest()

    graph = re.sub(rf"media/cache/{re.escape(stem)}[\w.-]*", content, plan["graph"])
    files = []
    for args in plan["inputs"]:
        for flag, value in zip(args, args[1:]):
            path = project_dir / value
            if flag == "-i" and path.is_file():
                st = path.stat()
                files.append([value, st.st_size, st.st_mtime_ns])
    raw = json.dumps([BUILD, media.version(), plan["window"], plan["inputs"], graph, codec, files])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def evict(store: Path, keep: set[Path]) -> None:
    """Keep this export's segments and mix and, of the others, the most recently used up to the same size again;
    delete the rest (a mix with its loudness note)."""
    room = sum(f.stat().st_size for f in keep if f.is_file())
    others = sorted((f for f in store.iterdir() if f.suffix in (".ts", ".wav") and f not in keep),
                    key=lambda f: f.stat().st_mtime, reverse=True)
    for f in others:
        room -= f.stat().st_size
        if room < 0:
            try:
                f.unlink()
                f.with_suffix(".json").unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Could not remove the cached segment %s: %s", f.name, exc)


def render_segments(media: Media, project: dict, project_dir: Path, clips: list[dict], opts: dict, plans: list[dict],
                    encoder: str, work_path: Path, on_progress: Callable[[float], None] | None = None,
                    cancelled: Callable[[], bool] | None = None, ass: str | None = None) -> tuple[str, dict | None]:
    """Render the segments side by side with the sound beside them, and join them into work_path; a segment kept
    from an earlier export under the same key is joined instead of rendered, and so is a kept mix. ass is the
    export's whole ASS script.
    Returns the encoder used (libx264 for every segment once the hardware encoder failed in one) and loudnorm's
    first-pass report (None when the mix is not normalised). Raises JoinFailed when only the join failed."""
    cache, stem, total = work_path.parent, work_path.stem, plans[0]["total"]
    store = cache / STORE
    store.mkdir(exist_ok=True)
    rate = frames_per_second(plans[0]["fps"])
    counts = [p["window"][1] - p["window"][0] for p in plans]
    shares = [n / sum(counts) * (1 - AUDIO_SHARE - JOIN_SHARE) for n in counts] + [AUDIO_SHARE, JOIN_SHARE]
    done = [0.0] * len(shares)
    lock = threading.Lock()

    def part(k: int, scale: float = 1.0, offset: float = 0.0) -> Callable[[float], None]:
        def report(f: float) -> None:
            with lock:
                done[k] = offset + f * scale
                if on_progress:
                    on_progress(sum(s * d for s, d in zip(shares, done)))
        return report

    stop = threading.Event()  # set when a segment or the sound failed: the others stop too
    user_cancelled = cancelled or (lambda: False)
    segs = [cache / f"{stem}-seg{k:02d}.ts" for k in range(len(plans))]
    scripts = [cache / f"{stem}-seg{k:02d}.graph.txt" for k in range(len(plans))]
    raw, wav, listing = cache / f"{stem}-mix.wav", cache / f"{stem}-loud.wav", cache / f"{stem}-segments.txt"
    sound_files = [cache / f"{stem}-sound{k}.txt" for k in range(3)]
    log.info("分段平行輸出：%d 段、%d 個同時，接縫在 %s", len(plans), workers(),
             ", ".join(f"{t:.3f}s" for t in seam_times(plans)))
    temp = [*segs, *scripts, raw, wav, listing, *sound_files, *write_segment_ass(project_dir, plans, ass)]
    used: list[Path] = list(segs)  # what the listing joins: a cached segment or the one just rendered
    mixes: list[Path] = []  # the kept mix this export joins, once sound() has it

    def pictures(enc: str) -> None:
        stop.clear()
        errors: list[AppError] = []
        codec = video_codec_args(media, project, opts, enc)
        reused: list[int] = []

        def one(k: int) -> None:
            if stop.is_set() or user_cancelled():
                return
            a, b = plans[k]["window"]
            kept = store / f"{segment_key(media, plans[k], codec, project_dir, stem)}.ts"
            if kept.is_file():
                os.utime(kept)  # most recently used: evict() keeps it longest
                used[k] = kept
                reused.append(k)
                part(k)(1.0)
                return
            tail = [*media.filter_script_args(Path("media/cache") / scripts[k].name), "-map", "[vout]",
                    "-frames:v", str(b - a), *codec, "-an", "-f", "mpegts", str(segs[k])]
            try:
                args, graph = media.fit_inputs(plans[k]["inputs"], plans[k]["graph"], tail, project_dir)
                scripts[k].write_text(graph, encoding="utf-8")
                media.run_progress(args + tail, (b - a) / rate, part(k), lambda: stop.is_set() or user_cancelled(),
                                   cwd=project_dir)
                segs[k].replace(kept)  # complete: kept even when another segment then fails
                used[k] = kept
            except AppError as exc:
                errors.append(exc)
                stop.set()

        with ThreadPoolExecutor(max_workers=workers()) as pool:
            list(pool.map(one, range(len(plans))))
        if user_cancelled():
            raise AppError("已取消", 499)
        failed = [e for e in errors if e.status != 499] or errors
        if failed:
            raise failed[0]
        log.info("分段快取：%d 段沿用上次輸出、%d 段重新輸出", len(reused), len(plans) - len(reused))

    def sound() -> tuple[Path, dict | None]:
        """The mix to join and loudnorm's report: kept from an earlier export under the same key, or mixed now."""
        plan = plan_audio_export(project, project_dir, clips, opts)
        key = segment_key(media, {**plan, "window": None}, ["sound", opts.get("loudness"), total], project_dir, stem)
        kept, note = store / f"{key}.wav", store / f"{key}.json"
        if kept.is_file() and note.is_file():
            os.utime(kept)
            mixes.append(kept)
            part(len(plans))(1.0)
            log.info("混音快取：沿用上次輸出")
            return kept, json.loads(note.read_text(encoding="utf-8"))
        try:
            report = _render_sound(media, plan, project_dir, opts, total, raw, wav, sound_files, part(len(plans)),
                                   user_cancelled)
        except AppError:
            stop.set()
            raise
        note.write_text(json.dumps(report), encoding="utf-8")
        (wav if report else raw).replace(kept)
        mixes.append(kept)
        log.info("混音快取：重新混音")
        return kept, report

    try:
        with ThreadPoolExecutor(max_workers=1) as side:
            mixed = side.submit(sound)
            try:
                pictures(encoder)
            except AppError as exc:
                if exc.status == 499 or encoder == "libx264" or mixed.done() and mixed.exception():
                    raise
                log.warning("Segment with %s failed (%s); rendering every segment again with libx264", encoder,
                            exc.message)
                encoder = "libx264"
                for f in segs:
                    f.unlink(missing_ok=True)
                pictures(encoder)
            mix, report = mixed.result()
        listing.write_text(join_listing(used, counts, plans[0]["fps"], cache), encoding="utf-8")
        tail = ["-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate(opts),
                "-ar", "48000", "-t", f"{total:.3f}", "-movflags", "+faststart", str(work_path)]
        try:
            media.run_progress(["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(mix), *tail], total,
                               part(len(plans) + 1), user_cancelled, cwd=project_dir)
        except AppError as exc:
            if exc.status == 499:
                raise
            raise JoinFailed(exc.message) from exc
        return encoder, report
    finally:
        for f in temp:
            f.unlink(missing_ok=True)
        evict(store, {f for f in [*used, *mixes] if f.parent == store})


def _render_sound(media: Media, plan: dict, project_dir: Path, opts: dict, total: float, raw: Path, loud: Path,
                  scripts: list[Path], on_progress: Callable[[float], None],
                  cancelled: Callable[[], bool]) -> dict | None:
    """Mix the sound-only `plan` once into raw (32-bit float WAV, lossless), then, for a loudness target, measure
    that file and write it normalised into loud. Returns loudnorm's report, or None (raw is the mix to use)."""
    target = opts.get("loudness")
    first = 1.0 if target is None else 0.6
    tail = [*media.filter_script_args(Path("media/cache") / scripts[0].name), "-map", "[aout]",
            "-t", f"{total:.3f}", "-c:a", "pcm_f32le", str(raw)]
    args, graph = media.fit_inputs(plan["inputs"], plan["graph"], tail, project_dir)
    scripts[0].write_text(graph, encoding="utf-8")
    media.run_progress(args + tail, total, lambda f: on_progress(f * first), cancelled, cwd=project_dir)
    if target is None:
        return None
    mix = {"inputs": [["-i", str(raw)]], "graph": "[0:a]anull[aout]", "total": total}
    report = measure_loudness(media, mix, project_dir, scripts[1], target, lambda f: on_progress(0.6 + f * 0.2),
                              cancelled)
    if report:
        tail = [*media.filter_script_args(Path("media/cache") / scripts[2].name), "-map", "[aloud]",
                "-t", f"{total:.3f}", "-c:a", "pcm_f32le", str(loud)]
        scripts[2].write_text(f"[0:a]{loudnorm_filter(target, report)},aresample=48000[aloud]", encoding="utf-8")
        media.run_progress(["-i", str(raw), *tail], total, lambda f: on_progress(0.8 + f * 0.2), cancelled,
                           cwd=project_dir)
    on_progress(1.0)
    return report
