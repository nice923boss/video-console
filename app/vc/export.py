"""Render the edit sequence into one MP4, plus the plain-text exports (SRT captions, shot list CSV).

The whole edit is a single ffmpeg filtergraph: every clip is normalised to the project size and
frame rate, runs joined by transitions are chained with xfade, runs are concatenated, picture-in-picture
clips are overlaid track by track, captions and placeholder slates are burnt in through one ASS file,
and all audio is mixed with amix.
ffmpeg runs with the project folder as working directory so inputs are short relative paths.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from pathlib import Path
from typing import Callable

from .captions import ass_text as _ass_text
from .captions import ass_time as _ass_time
from .captions import caption_ass
from .grade import build_cube, clean_grade, is_neutral
from .media import Media
from .sequence import XFADE_NAMES, build_sequence, total_duration
from .util import AppError, safe_filename

log = logging.getLogger("vc.export")

SLATE_COLOR = "0x15171a"
CAPTION_FONT = "Microsoft JhengHei"


def export_options(project: dict, settings_export: dict, overrides: dict | None) -> dict:
    """Merge app export defaults, project fades and per-job overrides into one option dict."""
    opts = {
        "quality": settings_export.get("quality", "high"),
        "encoder": "auto",
        "burnCaptions": bool(settings_export.get("burnCaptions", False)),
        "captionSize": int(settings_export.get("captionSize", 52)),
        "fadeIn": float(project["settings"].get("fadeIn") or 0.0),
        "fadeOut": float(project["settings"].get("fadeOut") or 0.0),
        "clipAudio": True,
        "fit": "contain",
    }
    for key, value in (overrides or {}).items():
        if key in opts and value is not None:
            opts[key] = value
    if opts["quality"] not in ("high", "standard", "draft"):
        opts["quality"] = "high"
    if opts["fit"] not in ("contain", "cover"):
        opts["fit"] = "contain"
    opts["captionSize"] = max(16, min(160, int(opts["captionSize"])))
    opts["fadeIn"] = max(0.0, min(10.0, float(opts["fadeIn"])))
    opts["fadeOut"] = max(0.0, min(10.0, float(opts["fadeOut"])))
    return opts


# ---------------------------------------------------------------- ASS (captions + slates)
_ASS_ALIGN = {"bottom": 2, "middle": 5, "top": 8}


def build_ass(project: dict, clips: list[dict], opts: dict, width: int, height: int) -> str | None:
    """ASS script with burnt-in captions (optional) and slates for shots that have no picture."""
    play_y = 1080
    play_x = max(1, round(play_y * width / height))
    size = opts["captionSize"]
    events: list[str] = []
    # Slates use the shot numbers of the shot list (not the sequence position), so they match the app.
    numbers = {s["id"]: i for i, s in enumerate(project.get("shots") or [], start=1)}
    tracks = project.get("tracks") or {}
    for n, clip in enumerate(clips, start=1):
        # A hidden picture track hides its slates too, as the preview does.
        if clip["source"] in ("video", "image") or tracks.get("videoHidden"):
            continue
        name = (clip["shot"].get("name") or "").strip()
        number = numbers.get(clip["shot"]["id"], n)
        state = "畫面檔案遺失" if clip["source"] == "missing" else "尚未產生畫面"
        text = (
            "{\\fad(200,200)}" + _ass_text(f"#{number}\u3000{name}" if name else f"鏡頭 {number}")
            + "\\N{\\fs40\\alpha&H40&}" + _ass_text(state)
        )
        # Keep the slate text off the frames where a transition still shows the neighbouring clip
        # (cut transitions have duration 0).
        start = clip["start"] + (clips[n - 2]["transition"]["duration"] if n > 1 else 0.0)
        end = clip["start"] + clip["length"] - clip["transition"]["duration"]
        if end - start < 0.3:
            start, end = clip["start"], clip["start"] + clip["length"]
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Slate,,0,0,0,,{text}")
    burn: list[dict] = []
    if opts["burnCaptions"] and not tracks.get("captionsHidden"):
        burn = [cap for cap in project.get("captions", [])
                if (cap.get("text") or "").strip() and float(cap.get("end", 0)) > float(cap.get("start", 0))]
    caption_styles, caption_events = caption_ass(project, burn, play_x, size)
    events += caption_events
    if not events:
        return None
    outline = max(2, round(size / 14))
    margin_v = round(play_y * 0.06)
    fmt = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding"
    )

    def style(name: str, fontsize: int, colour: str, align: int, bold: int = 1) -> str:
        return (
            f"Style: {name},{CAPTION_FONT},{fontsize},{colour},&H00FFFFFF,&H00000000,&H96000000,{bold},0,0,0,"
            f"100,100,0,0,1,{outline},1,{align},80,80,{margin_v},1"
        )

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_x}",
        f"PlayResY: {play_y}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        fmt,
        *caption_styles,
        style("Slate", 64, "&H00D9DEE3", _ASS_ALIGN["middle"], bold=0),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- filtergraph
def _fit_filter(fit: str, w: int, h: int) -> str:
    if fit == "cover":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def prepare_clips(project: dict, project_dir: Path) -> list[dict]:
    """The sequence's clips, with clips whose picture file vanished marked as 'missing'."""
    clips = build_sequence(project)
    if not clips:
        raise AppError("序列裡沒有任何鏡頭可以輸出（請確認鏡頭有勾選「放入序列」）")
    for clip in clips:
        take = clip["take"]
        if clip["source"] != "empty" and not (take and (project_dir / take["file"]).is_file()):
            clip["source"] = "missing"
    return clips


def _plan_overlays(project: dict, project_dir: Path, label: str, total: float, opts: dict,
                   inputs: list[list[str]], graph: list[str]) -> tuple[str, list[tuple[int, float, float, float]]]:
    """Lay the picture-in-picture clips over the picture, bottom track first (a higher track covers a lower
    one). Returns the new picture label and (input, start, length, volume) for each clip whose sound plays."""
    w, h, fps = int(project["settings"]["width"]), int(project["settings"]["height"]), int(project["settings"]["fps"])
    all_media = project.get("media") or {}
    sounds: list[tuple[int, float, float, float]] = []
    for layer in project.get("layers") or []:
        for item in project.get("overlays") or []:
            media = all_media.get(item.get("mediaId") or "")
            if item.get("layer") != layer.get("id") or not media or media.get("kind") not in ("image", "video"):
                continue
            start = float(item.get("start") or 0.0)
            length = min(float(item.get("length") or 0.0), total - start)
            volume = float(item.get("volume", 1.0))
            # Hiding a track only hides its picture and muting only silences it, as in the preview.
            loud = (opts["clipAudio"] and media["kind"] == "video" and bool(media.get("hasAudio"))
                    and not layer.get("muted") and volume > 0)
            if length <= 0.05 or (layer.get("hidden") and not loud) or not (project_dir / media["file"]).is_file():
                continue
            if media["kind"] == "video":
                inputs.append(["-ss", f"{float(item.get('in') or 0.0):.3f}", "-t", f"{length:.3f}", "-i", media["file"]])
            else:
                inputs.append(["-loop", "1", "-framerate", str(fps), "-t", f"{length:.3f}", "-i", media["file"]])
            idx = len(inputs) - 1
            if loud:
                sounds.append((idx, start, length, volume))
            if layer.get("hidden"):
                continue
            # The clip's frames start at its place on the timeline; before that and after its end the
            # overlay passes the picture through unchanged.
            chain = [f"setpts=PTS-STARTPTS+{start:.3f}/TB",
                     f"scale={max(2, round(w * float(item.get('w') or 0.35) / 2) * 2)}:-2:flags=lanczos", "format=rgba"]
            opacity = float(item.get("opacity", 1.0))
            if opacity < 1:
                chain.append(f"colorchannelmixer=aa={opacity:.3f}")
            x, y = round(w * float(item.get("x") or 0.0)), round(h * float(item.get("y") or 0.0))
            graph.append(f"[{idx}:v]{','.join(chain)}[pip{idx}]")
            graph.append(f"[{label}][pip{idx}]overlay=x={x}:y={y}:eof_action=pass[pv{idx}]")
            label = f"pv{idx}"
    return label, sounds


def active_adjustments(project: dict, total: float) -> list[tuple[float, float, dict]]:
    """(start, end, grade) of each adjustment layer that changes the picture, in timeline order."""
    if (project.get("tracks") or {}).get("adjustHidden"):
        return []
    out = []
    for item in project.get("adjustments") or []:
        start = float(item.get("start") or 0.0)
        end = min(start + float(item.get("length") or 0.0), total)
        grade = clean_grade(item.get("grade"))
        if item.get("hidden") or end - start <= 0.01 or is_neutral(grade):
            continue
        out.append((start, end, grade))
    return sorted(out, key=lambda span: span[0])


def plan_export(project: dict, project_dir: Path, clips: list[dict], opts: dict, ass_rel: str | None = None,
                luts: list[tuple[float, float, str]] | None = None) -> dict:
    """Build ffmpeg inputs and the filtergraph text for already prepared clips.

    luts holds (start, end, cube file) per adjustment layer; each grades the picture with its upper
    tracks during its span, before captions are burnt in."""
    total = total_duration(clips)
    w, h, fps = int(project["settings"]["width"]), int(project["settings"]["height"]), int(project["settings"]["fps"])
    inputs: list[list[str]] = []
    graph: list[str] = []
    video_index: dict[int, int] = {}

    for i, clip in enumerate(clips):
        length = clip["length"]
        if clip["source"] == "video":
            inputs.append(["-ss", f"{clip['in']:.3f}", "-t", f"{length:.3f}", "-i", clip["take"]["file"]])
        elif clip["source"] == "image":
            inputs.append(["-loop", "1", "-framerate", str(fps), "-t", f"{length:.3f}", "-i", clip["take"]["file"]])
        else:
            inputs.append(["-f", "lavfi", "-t", f"{length:.3f}", "-i", f"color=c={SLATE_COLOR}:s={w}x{h}:r={fps}"])
        idx = len(inputs) - 1
        video_index[i] = idx
        # setpts must come before fps: after setpts the link has no constant rate and xfade rejects it.
        graph.append(
            f"[{idx}:v]setpts=PTS-STARTPTS,{_fit_filter(opts['fit'], w, h)},setsar=1,fps={fps},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={length:.3f},trim=duration={length:.3f}[v{i}]"
        )

    # Runs of clips joined by transitions become xfade chains; runs are then concatenated.
    runs: list[list[int]] = [[0]]
    for i in range(1, len(clips)):
        if clips[i - 1]["transition"]["type"] == "cut":
            runs.append([i])
        else:
            runs[-1].append(i)
    run_labels: list[str] = []
    for r, run in enumerate(runs):
        label = f"v{run[0]}"
        run_start = clips[run[0]]["start"]
        for i in run[1:]:
            trans = clips[i - 1]["transition"]
            out = f"x{i}"
            graph.append(
                f"[{label}][v{i}]xfade=transition={XFADE_NAMES[trans['type']]}:duration={trans['duration']:.3f}:"
                f"offset={clips[i]['start'] - run_start:.3f}[{out}]"
            )
            label = out
        run_labels.append(label)
    if len(run_labels) == 1:
        joined = run_labels[0]
    else:
        graph.append("".join(f"[{lb}]" for lb in run_labels) + f"concat=n={len(run_labels)}:v=1:a=0[vjoin]")
        joined = "vjoin"
    tracks = project.get("tracks") or {}
    if tracks.get("videoHidden"):
        graph.append(f"[{joined}]drawbox=c=black:t=fill[vhide]")
        joined = "vhide"
    joined, pip_sounds = _plan_overlays(project, project_dir, joined, total, opts, inputs, graph)
    for i, (start, end, rel) in enumerate(luts or []):
        graph.append(f"[{joined}]lut3d=file={rel}:interp=tetrahedral:enable='between(t,{start:.3f},{end:.3f})'[adj{i}]")
        joined = f"adj{i}"

    post: list[str] = []
    if ass_rel:
        post.append(f"ass=filename={ass_rel}")
    if opts["fadeIn"] > 0:
        post.append(f"fade=t=in:st=0:d={opts['fadeIn']:.3f}")
    if opts["fadeOut"] > 0:
        post.append(f"fade=t=out:st={max(0.0, total - opts['fadeOut']):.3f}:d={opts['fadeOut']:.3f}")
    post.append("format=yuv420p")
    graph.append(f"[{joined}]{','.join(post)}[vout]")

    # Audio: clip sound from video takes plus the music/voice tracks, mixed without normalisation.
    # adelay/amix leave timestamps that later time-based filters (atrim, afade) misread, which made
    # the mix silent or endless; asetpts=N/SR/TB rebuilds them from the sample count.
    a_labels: list[str] = []
    fix_pts = "asetpts=N/SR/TB"
    aformat = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    if opts["clipAudio"] and not tracks.get("videoMuted"):
        for i, clip in enumerate(clips):
            if clip["source"] != "video" or not clip["take"].get("hasAudio"):
                continue
            length = clip["length"]
            fade_in = clips[i - 1]["transition"]["duration"] if i > 0 else 0.0
            fade_in = max(0.03, min(fade_in, length / 2))
            fade_out = max(0.03, min(clip["transition"]["duration"], length / 2))
            ms = int(round(clip["start"] * 1000))
            graph.append(
                f"[{video_index[i]}:a]{aformat},atrim=duration={length:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={length - fade_out:.3f}:d={fade_out:.3f},"
                f"adelay=delays={ms}:all=1,{fix_pts}[ca{i}]"
            )
            a_labels.append(f"ca{i}")
    for idx, start, length, volume in pip_sounds:
        graph.append(
            f"[{idx}:a]{aformat},atrim=duration={length:.3f},asetpts=PTS-STARTPTS,volume={volume:.3f},"
            f"adelay=delays={int(round(start * 1000))}:all=1,{fix_pts}[pa{idx}]"
        )
        a_labels.append(f"pa{idx}")
    for k, track in enumerate(project.get("audio", [])):
        media = project.get("media", {}).get(track.get("mediaId") or "")
        # Voice lines are the audio clips made for a caption; everything else sits on the music rows.
        row_muted = tracks.get("voiceMuted" if track.get("captionId") else "musicMuted")
        if track.get("muted") or row_muted or not media or not (project_dir / media["file"]).is_file():
            continue
        m_dur = float(media.get("duration") or 0.0)
        a_in = float(track.get("in") or 0.0)
        a_out = track.get("out")
        a_out = m_dur if a_out is None else min(float(a_out), m_dur or float(a_out))
        start = float(track.get("start") or 0.0)
        length = min(a_out - a_in, total - start)
        if length <= 0.05:
            continue
        inputs.append(["-i", media["file"]])
        idx = len(inputs) - 1
        fi = min(float(track.get("fadeIn") or 0.0), length / 2)
        fo = min(float(track.get("fadeOut") or 0.0), length / 2)
        chain = [aformat, f"atrim=start={a_in:.3f}:duration={length:.3f}", "asetpts=PTS-STARTPTS",
                 f"volume={float(track.get('volume', 1.0)):.3f}"]
        if fi > 0:
            chain.append(f"afade=t=in:st=0:d={fi:.3f}")
        if fo > 0:
            chain.append(f"afade=t=out:st={length - fo:.3f}:d={fo:.3f}")
        chain += [f"adelay=delays={int(round(start * 1000))}:all=1", fix_pts]
        graph.append(f"[{idx}:a]{','.join(chain)}[ta{k}]")
        a_labels.append(f"ta{k}")

    # A bare apad never ends (the export then hangs); whole_dur pads to exactly the video length.
    a_post = [f"apad=whole_dur={total:.3f}", f"atrim=duration={total:.3f}"]
    if opts["fadeIn"] > 0:
        a_post.append(f"afade=t=in:st=0:d={opts['fadeIn']:.3f}")
    if opts["fadeOut"] > 0:
        a_post.append(f"afade=t=out:st={max(0.0, total - opts['fadeOut']):.3f}:d={opts['fadeOut']:.3f}")
    if not a_labels:
        inputs.append(["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"])
        graph.append(f"[{len(inputs) - 1}:a]{aformat},{','.join(a_post)}[aout]")
    elif len(a_labels) == 1:
        graph.append(f"[{a_labels[0]}]{','.join(a_post)}[aout]")
    else:
        mix = "".join(f"[{lb}]" for lb in a_labels)
        graph.append(
            f"{mix}amix=inputs={len(a_labels)}:normalize=0:duration=longest,{fix_pts},{','.join(a_post)}[aout]"
        )

    return {
        "clips": clips,
        "inputs": inputs,
        "graph": ";\n".join(graph),
        "total": total,
        "width": w,
        "height": h,
        "fps": fps,
        "missing": sum(1 for c in clips if c["source"] == "missing"),
        "empty": sum(1 for c in clips if c["source"] == "empty"),
    }


def export_filename(project: dict) -> str:
    # safe_filename keeps only the last path part, so slashes in a project name must go first.
    name = str(project.get("name") or "").replace("/", "_").replace("\\", "_")
    stem = safe_filename(name, "video", 60).replace(" ", "_")
    return f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"


def render(
    media: Media,
    project: dict,
    project_dir: Path,
    opts: dict,
    encoder_pref: str = "auto",
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Render the project's sequence to exports/<name>_<time>.mp4 and return file info."""
    media.require()
    stamp = f"export-{int(time.time() * 1000)}"
    cache = project_dir / "media" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    ass_path = cache / f"{stamp}.ass"
    graph_path = cache / f"{stamp}.graph.txt"
    out_dir = project_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / export_filename(project)
    work_path = cache / f"{stamp}.mp4"  # rendered here and moved into exports/ only once complete

    clips = prepare_clips(project, project_dir)
    ass = build_ass(project, clips, opts, int(project["settings"]["width"]), int(project["settings"]["height"]))
    if ass:
        ass_path.write_text(ass, encoding="utf-8")
    lut_paths: list[Path] = []
    luts: list[tuple[float, float, str]] = []
    for i, (start, end, grade) in enumerate(active_adjustments(project, total_duration(clips))):
        lut_paths.append(cache / f"{stamp}-adj{i}.cube")
        lut_paths[-1].write_text(build_cube(grade), encoding="utf-8")
        luts.append((start, end, f"media/cache/{lut_paths[-1].name}"))
    plan = plan_export(project, project_dir, clips, opts, f"media/cache/{ass_path.name}" if ass else None, luts)
    graph_path.write_text(plan["graph"], encoding="utf-8")

    def args_for(encoder: str) -> list[str]:
        args: list[str] = []
        for inp in plan["inputs"]:
            args += inp
        args += media.filter_script_args(Path("media/cache") / graph_path.name)
        args += ["-map", "[vout]", "-map", "[aout]", "-t", f"{plan['total']:.3f}"]
        args += media.encoder_args(encoder, opts["quality"])
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(work_path)]
        return args

    encoder = media.pick_encoder(encoder_pref)
    started = time.time()
    try:
        try:
            media.run_progress(args_for(encoder), plan["total"], on_progress, cancelled, cwd=project_dir)
        except AppError as exc:
            if exc.status == 499 or encoder == "libx264":
                raise
            log.warning("Export with %s failed (%s); retrying with libx264", encoder, exc.message)
            encoder = "libx264"
            media.run_progress(args_for(encoder), plan["total"], on_progress, cancelled, cwd=project_dir)
        work_path.replace(out_path)
    finally:
        for tmp in (ass_path, graph_path, work_path, *lut_paths):
            tmp.unlink(missing_ok=True)
    log.info("Exported %s in %.1fs with %s", out_path.name, time.time() - started, encoder)
    return {
        "file": f"exports/{out_path.name}",
        "name": out_path.name,
        "duration": plan["total"],
        "size": out_path.stat().st_size,
        "encoder": encoder,
        "width": plan["width"],
        "height": plan["height"],
        "missing": plan["missing"],
        "empty": plan["empty"],
        "seconds": round(time.time() - started, 1),
    }


# ---------------------------------------------------------------- text exports
def _srt_time(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def captions_srt(project: dict) -> str:
    caps = sorted(
        (c for c in project.get("captions", []) if (c.get("text") or "").strip()),
        key=lambda c: float(c.get("start", 0)),
    )
    blocks = []
    for n, cap in enumerate(caps, start=1):
        blocks.append(f"{n}\n{_srt_time(float(cap['start']))} --> {_srt_time(float(cap['end']))}\n{cap['text'].strip()}\n")
    return "\n".join(blocks)


def shots_csv(project: dict) -> str:
    clips = {c["shot"]["id"]: c for c in build_sequence(project)}
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["序號", "名稱", "放入序列", "開始秒數", "長度秒數", "畫面描述", "動態描述", "運鏡", "Seed",
                     "路由", "圖片 Take", "影片 Take", "轉場", "備註"])
    for n, shot in enumerate(project["shots"], start=1):
        clip = clips.get(shot["id"], {})
        writer.writerow([
            n,
            shot.get("name", ""),
            "是" if shot.get("inSequence", True) else "否",
            f"{clip.get('start', 0):.2f}" if shot.get("inSequence", True) else "",
            f"{clip.get('length', shot.get('duration', 0)):.2f}",
            shot.get("prompt", ""),
            shot.get("motion", ""),
            shot.get("camera", ""),
            shot.get("seed", ""),
            shot.get("route", ""),
            shot.get("activeImage") or "",
            shot.get("activeVideo") or "",
            (shot.get("transition") or {}).get("type", "cut"),
            shot.get("notes", ""),
        ])
    return "\ufeff" + buf.getvalue()
