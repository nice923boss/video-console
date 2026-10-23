"""Turn a project's shot list into a timed edit sequence (shared by UI summary, export, storyboard).

Timing model: clips sit back to back; a non-cut transition of duration d overlaps the tail of
the outgoing clip, so the incoming clip starts d seconds earlier. The browser preview uses the
same rules (web/js/lib/sequence.js) so what you see is what export renders.
"""
from __future__ import annotations

from typing import Any

XFADE_NAMES = {
    "crossfade": "fade",
    "fade_black": "fadeblack",
    "fade_white": "fadewhite",
    "wipe_left": "wipeleft",
    "slide_left": "slideleft",
}


def video_take(project: dict, shot: dict) -> dict | None:
    tid = shot.get("activeVideo")
    take = project["takes"].get(tid) if tid else None
    if take and take.get("kind") == "video" and take.get("duration"):
        return take
    return None


def image_take(project: dict, shot: dict) -> dict | None:
    tid = shot.get("activeImage")
    take = project["takes"].get(tid) if tid else None
    return take if take and take.get("kind") == "image" else None


def clip_bounds(project: dict, shot: dict) -> tuple[float, float]:
    """Source in/out for a shot: trimmed range of its video, or 0..duration for a still."""
    vid = video_take(project, shot)
    if vid:
        dur = float(vid["duration"])
        t_in = min(max(0.0, float(shot.get("trimIn") or 0.0)), max(0.0, dur - 0.1))
        t_out = shot.get("trimOut")
        t_out = dur if t_out is None else min(dur, float(t_out))
        if t_out - t_in < 0.1:
            t_out = min(dur, t_in + 0.1)
        return t_in, t_out
    return 0.0, float(shot.get("duration") or 0.1)


def clip_speed(shot: dict, source: str) -> float:
    """Playback speed of a shot's video; stills always play at 1."""
    return min(4.0, max(0.25, float(shot.get("speed") or 1.0))) if source == "video" else 1.0


def audio_media(project: dict, item: dict) -> dict | None:
    """The file an audio clip plays: a library item, or the take a shot's sound was detached from."""
    return project["media"].get(item.get("mediaId")) or project["takes"].get(item.get("takeId"))


VOICE_TRACK = "voice"


def audio_track(item: dict) -> str:
    """Mirror of audioTrack in lib/sequence.js: "voice" for voice lines, else the music track the clip sits on."""
    if item.get("captionId") or item.get("track") == VOICE_TRACK:
        return VOICE_TRACK
    return item.get("track") or item["id"]


def build_sequence(project: dict, include_all: bool = False) -> list[dict[str, Any]]:
    """Return clips with start/length/in/out/speed and the transition into the next clip."""
    shots = [s for s in project["shots"] if include_all or s.get("inSequence", True)]
    clips: list[dict[str, Any]] = []
    for shot in shots:
        t_in, t_out = clip_bounds(project, shot)
        vid = video_take(project, shot)
        img = image_take(project, shot)
        source = "video" if vid else ("image" if img else "empty")
        speed = clip_speed(shot, source)
        clips.append(
            {
                "shot": shot,
                "source": source,
                "take": vid or img,
                "in": t_in,
                "out": t_out,
                "speed": speed,
                "length": (t_out - t_in) / speed,
            }
        )
    start = 0.0
    for i, clip in enumerate(clips):
        clip["start"] = round(start, 4)
        trans = dict(clip["shot"].get("transition") or {"type": "cut", "duration": 0.5})
        if i == len(clips) - 1 or trans.get("type", "cut") == "cut":
            trans = {"type": "cut", "duration": 0.0}
        else:
            limit = min(clip["length"], clips[i + 1]["length"]) * 0.5
            trans["duration"] = round(max(0.04, min(float(trans.get("duration", 0.5)), limit)), 4)
        clip["transition"] = trans
        start += clip["length"] - trans["duration"]
    return clips


def total_duration(clips: list[dict]) -> float:
    if not clips:
        return 0.0
    last = clips[-1]
    return round(last["start"] + last["length"], 4)


def summary(project: dict) -> dict:
    clips = build_sequence(project)
    rendered = sum(1 for c in clips if c["source"] == "video")
    stills = sum(1 for c in clips if c["source"] == "image")
    return {
        "clips": len(clips),
        "duration": total_duration(clips),
        "videos": rendered,
        "stills": stills,
        "empty": len(clips) - rendered - stills,
    }


def compose_prompt(*parts: str) -> str:
    """Join non-empty prompt fragments; style text is appended after the shot's own text."""
    cleaned = [p.strip().strip(",，") for p in parts if p and p.strip()]
    return "\n".join(cleaned)
