"""The export's frame grid: the project frame rate as ffmpeg takes it, and the frame each timeline time falls on.

Cuts, overlays and grades all start and end on these frames. Trimming each clip to its own length rounded every
clip on its own, and the error added up until a cut came a frame after the end of the clip laid over it, which
showed a frame of the shot below.
"""
from __future__ import annotations

import math
from fractions import Fraction


def frame_rate(fps: float) -> str:
    """The project frame rate as ffmpeg takes it: 29.97 is the NTSC rate 30000/1001."""
    return "30000/1001" if abs(fps - 29.97) < 0.01 else f"{fps:g}"


def frames_per_second(fps: float) -> float:
    """The exact rate frame_rate gives ffmpeg."""
    return float(Fraction(frame_rate(fps)))


def frame_at(t: float, fps: float) -> int:
    """The output frame timeline time t falls on: the nearest, a half rounding up as in ffmpeg."""
    return math.floor(t * frames_per_second(fps) + 0.5 + 1e-6)


def frame_bounds(start: float, end: float, fps: float) -> tuple[float, float]:
    """The times halfway before frame_at(start) and frame_at(end): the span's frames are the ones between, and a
    frame time a hair off its exact value still falls on the right side."""
    rate = frames_per_second(fps)
    return max(0.0, (frame_at(start, fps) - 0.5) / rate), (frame_at(end, fps) - 0.5) / rate


def clip_frames(clips: list[dict], total: float, fps: float) -> list[tuple[int, int]]:
    """(first, end) frame of each sequence clip: from the frame its start falls on to the frame its end falls on,
    so the clips add up to the timeline frame for frame and each cut lands where the clips laid over it begin and
    end. A clip ends where the next starts, plus the transition between them."""
    ends = [nxt["start"] + c["transition"]["duration"] for c, nxt in zip(clips, clips[1:])] + [total]
    return [(frame_at(c["start"], fps), frame_at(end, fps)) for c, end in zip(clips, ends)]
