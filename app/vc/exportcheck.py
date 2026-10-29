"""What an export would get wrong, listed before it runs: shots without a picture, missing files, things past the end.

The checks follow the same rules as export.plan_export, so a warning here means the exported file shows it:
a shot without a take becomes a slate, a vanished file a slate or silence, a disabled shot black.
Nothing here blocks the export; the export dialog shows the list and the user decides.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .cover import covered_shots
from .export import prepare_clips
from .sequence import VOICE_TRACK, audio_media, audio_track, total_duration


def export_check(project: dict, project_dir: Path) -> dict:
    clips = prepare_clips(project, project_dir)
    total = total_duration(clips)
    # No slate shows where the picture is hidden or a full-frame upper-track clip covers the whole shot.
    unseen = covered_shots(project) | {c["shot"]["id"] for c in clips if c["shot"].get("pictureHidden")}
    # Shot numbers of the shot list (as the app and the export slates show them), not sequence positions.
    numbers = {s["id"]: i for i, s in enumerate(project.get("shots") or [], start=1)}

    def shot_numbers(test: Callable[[dict], bool]) -> list[int]:
        return [numbers.get(c["shot"]["id"], 0) for c in clips if test(c)]

    media = project.get("media") or {}
    tracks = project.get("tracks") or {}
    # Muted clips and muted rows are left out of the mix, so their files do not matter.
    heard = [t for t in project.get("audio") or []
             if not t.get("muted") and not tracks.get("voiceMuted" if audio_track(t) == VOICE_TRACK else "musicMuted")]
    missing_audio = sorted({
        Path(m["file"]).name for t in heard
        if (m := audio_media(project, t)) and not (project_dir / m["file"]).is_file()
    })
    missing_overlays = sorted({
        Path(m["file"]).name for o in project.get("overlays") or []
        if not o.get("disabled") and (m := media.get(o.get("mediaId") or ""))
        and not (project_dir / m["file"]).is_file()
    })
    # Audio clips and captions are cut at the end of the picture; 0.05 s matches the export's own tolerance.
    past_end = total + 0.05
    audio_past = sum(1 for t in heard
                     if float(t.get("start") or 0.0) + _audio_length(project, t) > past_end)
    captions_past = sum(1 for c in project.get("captions") or []
                        if not c.get("disabled") and (c.get("text") or "").strip() and float(c.get("end") or 0.0) > past_end)
    return {
        "duration": round(total, 3),
        "empty": shot_numbers(lambda c: c["source"] == "empty" and c["shot"]["id"] not in unseen),
        "missing": shot_numbers(lambda c: c["source"] == "missing" and c["shot"]["id"] not in unseen),
        "disabled": shot_numbers(lambda c: bool(c["shot"].get("disabled"))),
        "missingAudio": missing_audio,
        "missingOverlays": missing_overlays,
        "audioPastEnd": audio_past,
        "captionsPastEnd": captions_past,
        "videoHidden": bool(tracks.get("videoHidden")),
    }


def _audio_length(project: dict, track: dict) -> float:
    """The audio clip's length on the timeline (out - in, the media duration standing in for an open out)."""
    media = audio_media(project, track) or {}
    a_in = float(track.get("in") or 0.0)
    a_out = track.get("out")
    a_out = float(media.get("duration") or 0.0) if a_out is None else float(a_out)
    return max(0.0, (a_out - a_in) / float(track.get("speed") or 1.0))
