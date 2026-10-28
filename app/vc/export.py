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
import json
import logging
import math
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from .captions import ass_text as _ass_text
from .captions import ass_time as _ass_time
from .captions import caption_ass, shown_captions
from .duck import duck_db, duck_expr, voice_spans
from .exportdir import file_ref, unique_path
from .grade import build_cube, clean_grade, is_neutral
from .keyframes import expr as kf_expr
from .keyframes import steps as kf_steps
from .media import ALPHA_DECODERS, AS_709, TAGS_709, TO_709, Media
from .sequence import VOICE_TRACK, XFADE_NAMES, audio_media, audio_track, build_sequence, total_duration
from .util import AppError, safe_filename
from .voiceclean import ensure_stems, mix_lines, stems_for

log = logging.getLogger("vc.export")

SLATE_COLOR = "0x15171a"
# The export works in BT.709 (QA-04): browsers and players read HD video without colour tags as BT.709, so
# that is how the preview shows video clips. Video sources are taken as BT.709 (AS_709) and every picture,
# slate and graded frame is converted to YUV with the BT.709 matrix (TO_709); the file is tagged to match (media.py).
CAPTION_FONT = "Microsoft JhengHei"
# Sound-only exports (for a voice actor or a podcast): the mix alone, without rendering the picture.
AUDIO_CODECS = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
}
# Loudness targets the mix can be brought to (EX-01), in LUFS: YouTube and streaming play back at -14, podcasts at -16.
LOUDNESS_TARGETS = (-14.0, -16.0)


def frame_rate(fps: float) -> str:
    """The project frame rate as ffmpeg takes it: 29.97 is the NTSC rate 30000/1001."""
    return "30000/1001" if abs(fps - 29.97) < 0.01 else f"{fps:g}"


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
        "fit": project["settings"].get("fit") or "contain",
        "format": "mp4",
        # A platform preset (YouTube, Reels...): the output size, scaled from the project size at the very end,
        # and a video bitrate cap in kbps. None and 0 keep the project size and the quality's own rate.
        "size": None,
        "maxrate": 0,
        # One of LOUDNESS_TARGETS to normalise the mix to; None (the default) leaves its level as mixed.
        "loudness": None,
    }
    for key, value in (overrides or {}).items():
        if key in opts and value is not None:
            opts[key] = value
    if opts["quality"] not in ("high", "standard", "draft"):
        opts["quality"] = "high"
    if opts["fit"] not in ("contain", "cover"):
        opts["fit"] = "contain"
    if opts["format"] not in ("mp4", *AUDIO_CODECS):
        opts["format"] = "mp4"
    opts["captionSize"] = max(16, min(160, int(opts["captionSize"])))
    opts["fadeIn"] = max(0.0, min(10.0, float(opts["fadeIn"])))
    opts["fadeOut"] = max(0.0, min(10.0, float(opts["fadeOut"])))
    if opts["size"]:
        try:
            sw, sh = (int(v) for v in opts["size"])
        except (TypeError, ValueError):
            raise AppError("輸出尺寸格式不正確") from None
        if not (16 <= sw <= 7680 and 16 <= sh <= 7680):
            raise AppError("輸出尺寸要在 16 到 7680 之間")
        opts["size"] = [sw // 2 * 2, sh // 2 * 2]
    else:
        opts["size"] = None
    opts["maxrate"] = max(0, min(200000, int(opts["maxrate"] or 0)))
    if opts["loudness"] is not None:
        try:
            opts["loudness"] = float(opts["loudness"])
        except (TypeError, ValueError):
            raise AppError("響度目標格式不正確") from None
        if opts["loudness"] not in LOUDNESS_TARGETS:
            raise AppError("響度目標只能是 -14 或 -16 LUFS")
    return opts


def native_size(project: dict, opts: dict) -> tuple[dict, dict]:
    """An output size of the project's own shape (EX-12) becomes the size the whole edit is laid out at, so a
    sharper source (an upscaled take) is fitted straight to it instead of to the smaller project size first.
    Positions are fractions of the frame and captions follow the frame height, so the layout stays the same.
    Another shape keeps the scale of the finished picture, where the fit choice adds bars or crops."""
    size = opts.get("size")
    s = project["settings"]
    if not size or abs(size[0] / size[1] - int(s["width"]) / int(s["height"])) > 0.01:
        return project, opts
    return {**project, "settings": {**s, "width": size[0], "height": size[1]}}, {**opts, "size": None}


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
    if opts["burnCaptions"]:
        burn = [cap for cap in shown_captions(project)
                if (cap.get("text") or "").strip() and float(cap.get("end", 0)) > float(cap.get("start", 0))
                and not cap.get("disabled")]
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


MASK_BLUR = 0.04  # blur radius at strength 1, a fraction of the frame height (web/js/lib/masks.js matches)
MASK_BLOCK = 0.06  # mosaic block at strength 1, a fraction of the frame height


def _even(v: float) -> int:
    return int(round(v / 2)) * 2


def mask_box(m: dict, w: int, h: int) -> tuple[int, int, int, int]:
    """A mask's crop in frame pixels, (width, height, x, y), even for yuv420p and inside the frame."""
    cw, ch = max(4, _even(m["w"] * w)), max(4, _even(m["h"] * h))
    return cw, ch, min(_even(m["x"] * w), w - cw), min(_even(m["y"] * h), h - ch)


def _mask_chains(label: str, masks: list[dict], w: int, h: int) -> list[str]:
    """Blur or mosaic boxes (GR-12) over [label + "m"], a clip fitted to the frame, writing [label]. Each box is
    cut out, blurred or pixelated, and laid back in place; later boxes see the earlier ones."""
    chains = []
    cur = f"{label}m"
    for k, m in enumerate(masks):
        cw, ch, cx, cy = mask_box(m, w, h)
        if m["mode"] == "mosaic":
            # Blocks of b pixels from the box's corner: averaged down, blown up square, cut back to the box.
            b = max(2, round(m["strength"] * h * MASK_BLOCK))
            sw, sh = -(-cw // b), -(-ch // b)
            fx = f"scale={sw}:{sh}:flags=area,scale={sw * b}:{sh * b}:flags=neighbor,crop={cw}:{ch}:0:0"
        else:
            # boxblur's chroma planes are half the box, and its radius may be at most half a plane.
            r = max(1, min(round(m["strength"] * h * MASK_BLUR), min(cw, ch) // 4))
            fx = f"boxblur={r}:2"
        out = label if k == len(masks) - 1 else f"{label}m{k}"
        chains.append(f"[{cur}]split[{label}b{k}][{label}c{k}]")
        chains.append(f"[{label}c{k}]crop={cw}:{ch}:{cx}:{cy},{fx}[{label}f{k}]")
        chains.append(f"[{label}b{k}][{label}f{k}]overlay={cx}:{cy}[{out}]")
        cur = out
    return chains


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
                   inputs: list[list[str]], graph: list[str],
                   picture: bool = True) -> tuple[str, list[tuple[int, float, float, str]]]:
    """Lay the picture-in-picture clips over the picture, bottom track first (a higher track covers a lower
    one). Returns the new picture label and (input, start, length, volume expression) for each clip whose
    sound plays. Keyframed position, size and opacity follow the timeline time t (T in geq).
    With picture False only the clips whose sound plays are opened and nothing is laid over."""
    w, h, fps = int(project["settings"]["width"]), int(project["settings"]["height"]), float(project["settings"]["fps"])
    all_media = project.get("media") or {}
    sounds: list[tuple[int, float, float, str]] = []
    for layer in project.get("layers") or []:
        for item in project.get("overlays") or []:
            media = all_media.get(item.get("mediaId") or "")
            if (item.get("layer") != layer.get("id") or not media or media.get("kind") not in ("image", "video")
                    or item.get("disabled")):
                continue
            start = float(item.get("start") or 0.0)
            length = min(float(item.get("length") or 0.0), total - start)
            keyed = {k for kf in item.get("keyframes") or [] for k in kf if k != "t"}
            volume = float(item.get("volume", 1.0))
            # Hiding a track only hides its picture and muting only silences it, as in the preview.
            loud = (opts["clipAudio"] and media["kind"] == "video" and bool(media.get("hasAudio"))
                    and not layer.get("muted") and (volume > 0 or "volume" in keyed))
            if length <= 0.05 or (layer.get("hidden") and not loud) or not (project_dir / media["file"]).is_file():
                continue
            if not picture and not loud:
                continue
            if media["kind"] == "video":
                decoder = ALPHA_DECODERS.get(media.get("alphaCodec") or "")
                inputs.append(["-ss", f"{float(item.get('in') or 0.0):.3f}", "-t", f"{length:.3f}",
                               *(["-c:v", decoder] if decoder else []), "-i", media["file"]])
            else:
                inputs.append(["-loop", "1", "-framerate", frame_rate(fps), "-t", f"{length:.3f}", "-i", media["file"]])
            idx = len(inputs) - 1
            if loud:
                sounds.append((idx, start, length, kf_expr(item, "volume", "t", 0.0, 1.0)))
            if layer.get("hidden") or not picture:
                continue
            # The clip's frames start at its place on the timeline; before that and after its end the
            # overlay passes the picture through unchanged.
            chain = [f"setpts=PTS-STARTPTS+{start:.3f}/TB"] + ([AS_709] if media["kind"] == "video" else [])
            chroma = item.get("chroma") or {}
            if chroma.get("on"):
                # Keyed on the clip's own pixels, before scaling blurs the edge of the colour.
                chain.append(f"chromakey=color=0x{chroma['color'][1:]}:similarity={float(chroma['similarity']):.3f}:"
                             f"blend={float(chroma['blend']):.3f}")
            if "opacity" in keyed:
                # geq keeps the frame size it first saw, so it runs before a changing scale.
                op = kf_expr(item, "opacity", "T", start, 1.0)
                chain += ["format=rgba", f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({op})'"]
            if "w" in keyed:
                chain += [f"scale=w='max(2,2*trunc({w}*({kf_expr(item, 'w', 't', start, 0.35)})/2))':h=-2:"
                          "eval=frame:flags=lanczos", "setsar=1"]
            else:
                chain.append(f"scale={max(2, round(w * float(item.get('w') or 0.35) / 2) * 2)}:-2:flags=lanczos")
            chain.append("format=rgba")
            opacity = float(item.get("opacity", 1.0))
            if opacity < 1 and "opacity" not in keyed:
                chain.append(f"colorchannelmixer=aa={opacity:.3f}")
            chain.append("scale=out_color_matrix=bt709:out_range=tv,format=yuva420p")  # as TO_709, keeping alpha
            if keyed & {"x", "y"}:
                x = f"'{w}*({kf_expr(item, 'x', 't', start)})'"
                y = f"'{h}*({kf_expr(item, 'y', 't', start)})'"
            else:
                x, y = round(w * float(item.get("x") or 0.0)), round(h * float(item.get("y") or 0.0))
            graph.append(f"[{idx}:v]{','.join(chain)}[pip{idx}]")
            graph.append(f"[{label}][pip{idx}]overlay=x={x}:y={y}:eof_action=pass[pv{idx}]")
            label = f"pv{idx}"
    return label, sounds


def active_adjustments(project: dict, total: float) -> list[tuple[float, float, dict]]:
    """(start, end, grade) of each adjustment layer that changes the picture, in timeline order.

    A keyframed grade becomes a run of short spans, each holding the grade at its middle (see kf_steps)."""
    if (project.get("tracks") or {}).get("adjustHidden"):
        return []
    out = []
    for item in project.get("adjustments") or []:
        start = float(item.get("start") or 0.0)
        end = min(start + float(item.get("length") or 0.0), total)
        if item.get("hidden") or end - start <= 0.01:
            continue
        for a, b, grade in kf_steps(item, "grade", start, end, float(project["settings"]["fps"])):
            grade = clean_grade(grade)
            if b - a > 0.001 and not is_neutral(grade):
                out.append((a, b, grade))
    return sorted(out, key=lambda span: span[0])


def shot_grades(clips: list[dict]) -> list[tuple[int, dict]]:
    """(clip index, grade) of each shown video or still clip whose own grade changes the picture (GR-01)."""
    out = []
    for i, clip in enumerate(clips):
        shot = clip["shot"]
        if clip["source"] in ("video", "image") and not shot.get("disabled") and shot.get("grade"):
            grade = clean_grade(shot["grade"])
            if not is_neutral(grade):
                out.append((i, grade))
    return out


def _atempo(speed: float) -> str:
    """Filters (with a trailing comma) that play sound at speed without changing its pitch."""
    if abs(speed - 1) < 1e-6:
        return ""
    if speed < 0.5:  # one atempo goes no lower than half speed
        return f"atempo={math.sqrt(speed):.4f}," * 2
    return f"atempo={speed:.4f},"


def plan_export(project: dict, project_dir: Path, clips: list[dict], opts: dict, ass_rel: str | None = None,
                luts: list[tuple[float, float, str]] | None = None, shot_luts: dict[int, str] | None = None) -> dict:
    """Build ffmpeg inputs and the filtergraph text for already prepared clips.

    luts holds (start, end, cube file) per adjustment layer; each grades the picture with its upper
    tracks during its span, before captions are burnt in (after, when the project's gradeCaptions is on).
    shot_luts maps a clip index to the cube file of its shot's own grade, applied to that clip's source
    before it is fitted to the frame, so letterbox bars stay black."""
    total = total_duration(clips)
    w, h, fps = int(project["settings"]["width"]), int(project["settings"]["height"]), float(project["settings"]["fps"])
    inputs: list[list[str]] = []
    graph: list[str] = []
    video_index: dict[int, int] = {}

    for i, clip in enumerate(clips):
        length = clip["length"]
        speed = float(clip.get("speed") or 1.0)
        disabled = bool(clip["shot"].get("disabled"))
        if disabled:
            # A disabled shot keeps its place on the timeline as black and silence.
            inputs.append(["-f", "lavfi", "-t", f"{length:.3f}", "-i", f"color=c=black:s={w}x{h}:r={frame_rate(fps)}"])
        elif clip["source"] == "video":
            inputs.append(["-ss", f"{clip['in']:.3f}", "-t", f"{length * speed:.3f}", "-i", clip["take"]["file"]])
        elif clip["source"] == "image":
            inputs.append(["-loop", "1", "-framerate", frame_rate(fps), "-t", f"{length:.3f}", "-i", clip["take"]["file"]])
        else:
            inputs.append(["-f", "lavfi", "-t", f"{length:.3f}", "-i", f"color=c={SLATE_COLOR}:s={w}x{h}:r={frame_rate(fps)}"])
        idx = len(inputs) - 1
        video_index[i] = idx
        # setpts must come before fps: after setpts the link has no constant rate and xfade rejects it.
        pts = "PTS-STARTPTS" if speed == 1 or disabled else f"(PTS-STARTPTS)/{speed:.4f}"
        lut = f"lut3d=file={shot_luts[i]}:interp=tetrahedral," if shot_luts and i in shot_luts else ""
        if clip["source"] == "video":
            lut = f"{AS_709},{lut}"
        # Masks sit on the fitted frame, so a box drawn over the preview lands on the same pixels.
        masks = (clip["shot"].get("masks") or []) if clip["source"] in ("video", "image") and not disabled else []
        graph.append(
            f"[{idx}:v]setpts={pts},{lut}{_fit_filter(opts['fit'], w, h)},setsar=1,fps={frame_rate(fps)},{TO_709},"
            f"tpad=stop_mode=clone:stop_duration={length:.3f},trim=duration={length:.3f}[v{i}{'m' if masks else ''}]"
        )
        graph.extend(_mask_chains(f"v{i}", masks, w, h))

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
    graded_caps = bool(ass_rel and project["settings"].get("gradeCaptions"))
    if graded_caps:
        graph.append(f"[{joined}]ass=filename={ass_rel}[caps]")
        joined = "caps"
    for i, (start, end, rel) in enumerate(luts or []):
        graph.append(f"[{joined}]lut3d=file={rel}:interp=tetrahedral:enable='between(t,{start:.3f},{end:.3f})'[adj{i}]")
        joined = f"adj{i}"

    post: list[str] = []
    if ass_rel and not graded_caps:
        post.append(f"ass=filename={ass_rel}")
    if opts["fadeIn"] > 0:
        post.append(f"fade=t=in:st=0:d={opts['fadeIn']:.3f}")
    if opts["fadeOut"] > 0:
        post.append(f"fade=t=out:st={max(0.0, total - opts['fadeOut']):.3f}:d={opts['fadeOut']:.3f}")
    out_w, out_h = opts.get("size") or (w, h)
    if (out_w, out_h) != (w, h):
        post.append(f"{_fit_filter(opts['fit'], out_w, out_h)},setsar=1")
    post.append(TO_709)
    graph.append(f"[{joined}]{','.join(post)}[vout]")

    _plan_audio(project, project_dir, clips, opts, total, inputs, graph, video_index, pip_sounds)

    return {
        "clips": clips,
        "inputs": inputs,
        "graph": ";\n".join(graph),
        "total": total,
        "width": out_w,
        "height": out_h,
        "fps": fps,
        "missing": sum(1 for c in clips if c["source"] == "missing"),
        "empty": sum(1 for c in clips if c["source"] == "empty"),
    }


def _clip_sounds(clip: dict) -> bool:
    """Whether a picture clip's own sound goes into the mix (when clip audio is on and not muted)."""
    shot = clip["shot"]
    return (clip["source"] == "video" and bool(clip["take"].get("hasAudio")) and not shot.get("disabled")
            and not shot.get("audioDetached") and float(shot.get("volume", 1.0)) > 0)


def _plan_audio(project: dict, project_dir: Path, clips: list[dict], opts: dict, total: float,
                inputs: list[list[str]], graph: list[str], video_index: dict[int, int],
                pip_sounds: list[tuple[int, float, float, str]]) -> None:
    """Mix every sound of the sequence into [aout]; video_index maps a clip to its input."""
    tracks = project.get("tracks") or {}
    # Audio: clip sound from video takes plus the music/voice tracks, mixed without normalisation.
    # adelay/amix leave timestamps that later time-based filters (atrim, afade) misread, which made
    # the mix silent or endless; asetpts=N/SR/TB rebuilds them from the sample count.
    a_labels: list[str] = []
    fix_pts = "asetpts=N/SR/TB"
    aformat = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    if opts["clipAudio"] and not tracks.get("videoMuted"):
        for i, clip in enumerate(clips):
            if not _clip_sounds(clip):
                continue
            length = clip["length"]
            fade_in = clips[i - 1]["transition"]["duration"] if i > 0 else 0.0
            fade_in = max(0.03, min(fade_in, length / 2))
            fade_out = max(0.03, min(clip["transition"]["duration"], length / 2))
            ms = int(round(clip["start"] * 1000))
            gain = float(clip["shot"].get("volume", 1.0))
            gain_filter = f"volume={gain:.4f}," if gain != 1.0 else ""
            graph.append(
                f"[{video_index[i]}:a]{aformat},{gain_filter}{_atempo(clip.get('speed') or 1.0)}atrim=duration={length:.3f},"
                f"asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={length - fade_out:.3f}:d={fade_out:.3f},"
                f"adelay=delays={ms}:all=1,{fix_pts}[ca{i}]"
            )
            a_labels.append(f"ca{i}")
    for idx, start, length, volume in pip_sounds:
        graph.append(
            f"[{idx}:a]{aformat},atrim=duration={length:.3f},asetpts=PTS-STARTPTS,volume='{volume}':eval=frame,"
            f"adelay=delays={int(round(start * 1000))}:all=1,{fix_pts}[pa{idx}]"
        )
        a_labels.append(f"pa{idx}")
    heard = opts["clipAudio"] and not tracks.get("videoMuted")
    spans = voice_spans(project, [(c["start"], c["start"] + c["length"]) for c in clips if heard and _clip_sounds(c)])
    bgm = set(tracks.get("bgmRows") or [])
    for k, track in enumerate(project.get("audio", [])):
        media = audio_media(project, track)
        # Voice lines are the audio clips made for a caption or dragged onto the voice rows; everything else is music.
        row_muted = tracks.get("voiceMuted" if audio_track(track) == VOICE_TRACK else "musicMuted")
        if track.get("muted") or row_muted or not media or not (project_dir / media["file"]).is_file():
            continue
        m_dur = float(media.get("duration") or 0.0)
        a_in = float(track.get("in") or 0.0)
        a_out = track.get("out")
        a_out = m_dur if a_out is None else min(float(a_out), m_dur or float(a_out))
        start = float(track.get("start") or 0.0)
        speed = float(track.get("speed") or 1.0)  # the detached sound of a sped shot
        length = min((a_out - a_in) / speed, total - start)
        if length <= 0.05:
            continue
        # A clip with 降噪 / 去回音 or one channel picked plays its stems (voiceclean.py), rendered before planning.
        stems = stems_for(project, project_dir, track)
        inputs.append(["-i", stems["file"] if stems else media["file"]])
        idx = len(inputs) - 1
        head = f"{idx}:a"
        if stems:
            graph.extend(mix_lines(track, head, f"vc{k}"))
            head = f"vc{k}"
        fi = min(float(track.get("fadeIn") or 0.0), length / 2)
        fo = min(float(track.get("fadeOut") or 0.0), length / 2)
        chain = [aformat, f"atrim=start={a_in - (stems['from'] if stems else 0.0):.3f}:duration={length * speed:.3f}", "asetpts=PTS-STARTPTS",
                 *([_atempo(speed).rstrip(",")] if _atempo(speed) else []),
                 f"volume='{kf_expr(track, 'volume', 't', 0.0, 1.0)}':eval=frame"]
        duck = duck_expr(spans, start, length, duck_db(project["settings"])) if audio_track(track) in bgm else None
        if duck:
            chain.append(f"volume='{duck}':eval=frame")
        if fi > 0:
            chain.append(f"afade=t=in:st=0:d={fi:.3f}")
        if fo > 0:
            chain.append(f"afade=t=out:st={length - fo:.3f}:d={fo:.3f}")
        chain += [f"adelay=delays={int(round(start * 1000))}:all=1", fix_pts]
        graph.append(f"[{head}]{','.join(chain)}[ta{k}]")
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


def plan_audio_export(project: dict, project_dir: Path, clips: list[dict], opts: dict) -> dict:
    """plan_export without the picture: only the inputs whose sound is heard, mixed into [aout]."""
    total = total_duration(clips)
    inputs: list[list[str]] = []
    graph: list[str] = []
    video_index: dict[int, int] = {}
    for i, clip in enumerate(clips):
        if _clip_sounds(clip):
            speed = float(clip.get("speed") or 1.0)
            inputs.append(["-ss", f"{clip['in']:.3f}", "-t", f"{clip['length'] * speed:.3f}", "-i", clip["take"]["file"]])
            video_index[i] = len(inputs) - 1
    _, pip_sounds = _plan_overlays(project, project_dir, "", total, opts, inputs, graph, picture=False)
    _plan_audio(project, project_dir, clips, opts, total, inputs, graph, video_index, pip_sounds)
    return {
        "clips": clips,
        "inputs": inputs,
        "graph": ";\n".join(graph),
        "total": total,
        "width": 0,
        "height": 0,
        "fps": float(project["settings"]["fps"]),
        "missing": sum(1 for c in clips if c["source"] == "missing"),
        "empty": 0,
    }


def loudnorm_filter(target: float, measured: dict | None = None) -> str:
    """ffmpeg's loudnorm aiming at `target` LUFS under a -1 dBTP ceiling. Given the first pass's report it applies one
    fixed gain (linear), unless that gain would push the peaks over the ceiling; loudnorm then compresses instead."""
    f = f"loudnorm=I={target:g}:TP=-1:LRA=11"
    if measured:
        f += (f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
              f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
              f":offset={measured['target_offset']}:linear=true")
    return f


def measure_loudness(media: Media, plan: dict, project_dir: Path, script: Path, target: float,
                     on_progress: Callable[[float], None] | None = None,
                     cancelled: Callable[[], bool] | None = None) -> dict | None:
    """loudnorm's first pass (EX-01): the sound-only `plan` through loudnorm into nothing. Returns the report loudnorm
    logs (input_i, input_tp, input_lra, input_thresh, target_offset), or None for a mix too quiet to measure."""
    script.write_text(f"{plan['graph']};\n[aout]{loudnorm_filter(target)}:print_format=json[aloud]", encoding="utf-8")
    args = [arg for inp in plan["inputs"] for arg in inp]
    args += media.filter_script_args(Path("media/cache") / script.name)
    args += ["-map", "[aloud]", "-t", f"{plan['total']:.3f}", "-f", "null", "-"]
    text = media.run_progress(args, plan["total"], on_progress, cancelled, cwd=project_dir, loglevel="info")
    try:
        report = json.loads(re.findall(r"\{[^{}]*\}", text)[-1])
        level = float(report["input_i"])  # "-inf" for silence
    except (IndexError, KeyError, ValueError):
        raise AppError("無法測量混音響度", 500, detail=text[-2000:]) from None
    return report if level > -70 else None


def export_filename(project: dict, ext: str = "mp4") -> str:
    # safe_filename keeps only the last path part, so slashes in a project name must go first.
    name = str(project.get("name") or "").replace("/", "_").replace("\\", "_")
    stem = safe_filename(name, "video", 60).replace(" ", "_")
    return f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}.{ext}"


def render(
    media: Media,
    project: dict,
    project_dir: Path,
    opts: dict,
    encoder_pref: str = "auto",
    on_progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    out_dir: Path | None = None,
) -> dict:
    """Render the project's sequence to <out_dir>/<name>_<time>.mp4 (default the project's exports/) and
    return file info. opts["format"] wav or mp3 renders only the sound."""
    media.require()
    project, opts = native_size(project, opts)
    fmt = opts.get("format", "mp4")
    sound_only = fmt in AUDIO_CODECS
    stamp = f"export-{int(time.time() * 1000)}"
    cache = project_dir / "media" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    ass_path = cache / f"{stamp}.ass"
    graph_path = cache / f"{stamp}.graph.txt"
    out_dir = out_dir or project_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = unique_path(out_dir, export_filename(project, fmt))
    part = out_dir / f".{out_path.name}.part"
    work_path = cache / f"{stamp}.{fmt}"  # rendered here and moved to out_dir only once complete

    for item in project.get("audio") or []:  # the stems _plan_audio plays for 降噪 / 去回音 / one channel
        src = audio_media(project, item)
        if not item.get("muted") and src and (project_dir / src["file"]).is_file():
            ensure_stems(media, project, project_dir, item, cancelled)
    clips = prepare_clips(project, project_dir)
    ass = None if sound_only else build_ass(project, clips, opts, int(project["settings"]["width"]),
                                            int(project["settings"]["height"]))
    if ass:
        ass_path.write_text(ass, encoding="utf-8")
    lut_paths: list[Path] = []
    luts: list[tuple[float, float, str]] = []
    for i, (start, end, grade) in enumerate([] if sound_only else active_adjustments(project, total_duration(clips))):
        lut_paths.append(cache / f"{stamp}-adj{i}.cube")
        lut_paths[-1].write_text(build_cube(grade), encoding="utf-8")
        luts.append((start, end, f"media/cache/{lut_paths[-1].name}"))
    shot_luts: dict[int, str] = {}
    for i, grade in [] if sound_only else shot_grades(clips):
        lut_paths.append(cache / f"{stamp}-shot{i}.cube")
        lut_paths[-1].write_text(build_cube(grade), encoding="utf-8")
        shot_luts[i] = f"media/cache/{lut_paths[-1].name}"
    plan = (plan_audio_export(project, project_dir, clips, opts) if sound_only
            else plan_export(project, project_dir, clips, opts, f"media/cache/{ass_path.name}" if ass else None, luts,
                             shot_luts))
    loud_path = cache / f"{stamp}.loudness.txt"
    report: dict | None = None  # loudnorm's first-pass report when the mix is normalised (EX-01)

    def args_for(encoder: str) -> list[str]:
        args: list[str] = []
        for inp in plan["inputs"]:
            args += inp
        args += media.filter_script_args(Path("media/cache") / graph_path.name)
        a_out = "[aloud]" if report else "[aout]"
        if sound_only:
            return args + ["-map", a_out, "-t", f"{plan['total']:.3f}", *AUDIO_CODECS[fmt], "-ar", "48000",
                           str(work_path)]
        args += ["-map", "[vout]", "-map", a_out, "-t", f"{plan['total']:.3f}"]
        args += [*media.encoder_args(encoder, opts["quality"], opts.get("maxrate") or 0), *TAGS_709]
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(work_path)]
        return args

    encoder = AUDIO_CODECS[fmt][1] if sound_only else media.pick_encoder(encoder_pref)
    started = time.time()
    target = opts.get("loudness")
    progress = on_progress
    try:
        graph = plan["graph"]
        if target is not None:
            # Measuring takes the first 30% of the progress bar, the render the rest.
            report = measure_loudness(media, plan if sound_only else plan_audio_export(project, project_dir, clips, opts),
                                      project_dir, loud_path, target,
                                      on_progress and (lambda f: on_progress(f * 0.3)), cancelled)
            progress = on_progress and (lambda f: on_progress(0.3 + f * 0.7))
        if report:
            graph += f";\n[aout]{loudnorm_filter(target, report)},aresample=48000[aloud]"
        graph_path.write_text(graph, encoding="utf-8")
        try:
            media.run_progress(args_for(encoder), plan["total"], progress, cancelled, cwd=project_dir)
        except AppError as exc:
            if exc.status == 499 or encoder == "libx264" or sound_only:
                raise
            log.warning("Export with %s failed (%s); retrying with libx264", encoder, exc.message)
            encoder = "libx264"
            media.run_progress(args_for(encoder), plan["total"], progress, cancelled, cwd=project_dir)
        shutil.move(work_path, part)  # out_dir may be on another drive, where a plain rename fails
        part.replace(out_path)
    finally:
        for tmp in (ass_path, graph_path, loud_path, work_path, part, *lut_paths):
            tmp.unlink(missing_ok=True)
    log.info("Exported %s in %.1fs with %s", out_path.name, time.time() - started, encoder)
    return {
        "file": file_ref(out_path, project_dir),
        "name": out_path.name,
        "duration": plan["total"],
        "size": out_path.stat().st_size,
        "encoder": encoder,
        "width": plan["width"],
        "height": plan["height"],
        "missing": plan["missing"],
        "empty": plan["empty"],
        "seconds": round(time.time() - started, 1),
        # measured None: the mix was too quiet to normalise; linear False: loudnorm had to compress to keep the peaks.
        "loudness": None if target is None else {
            "target": target,
            "measured": round(float(report["input_i"]), 1) if report else None,
            "linear": bool(report) and float(report["input_tp"]) + target - float(report["input_i"]) <= -1,
        },
    }


# ---------------------------------------------------------------- text exports
def _srt_time(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def captions_srt(project: dict, track: str = "") -> str:
    """The captions as .srt; track "main" keeps the main 字幕 row only, a caption track id that row only."""
    rows = {t["id"] for t in project.get("captionTracks") or []}

    def on_track(c: dict) -> bool:
        row = c.get("track") if c.get("track") in rows else None
        return not track or row == (None if track == "main" else track)

    caps = sorted(
        (c for c in project.get("captions", []) if (c.get("text") or "").strip() and not c.get("disabled")
         and not c.get("title") and on_track(c)),
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
