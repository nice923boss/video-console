"""Background music ducking (AU-02): music on the rows marked 背景音樂 dips while a voice plays.

What counts as voice is what the user marked: a clip's 人聲 switch, or a whole row (the 配音 rows, a music
track, the 原聲 row). Only the 背景音樂 rows dip, so sound effects on other rows keep their level. The dip follows
the clips' spans on the timeline, not the waveform: a pause inside a voice clip keeps the music down.
Mirror of web/js/lib/duck.js."""
from __future__ import annotations

from .sequence import VOICE_TRACK, audio_media, audio_track

SOURCE_ROW = "source"  # the 原聲 row: the picture clips' own sound
ATTACK = 0.3  # seconds the music takes to go down, ending as the voice starts
RELEASE = 0.8  # seconds it takes to come back once the voice ends
GAP = ATTACK + RELEASE  # voices closer than this share one dip, so the music does not pump between lines
DEFAULT_DB = 10.0
DEFAULT_SPEECH_ROWS = (VOICE_TRACK,)


def speech_rows(tracks: dict) -> list[str]:
    """Rows whose every clip is voice; never set: the 配音 rows."""
    rows = tracks.get("speechRows")
    return list(DEFAULT_SPEECH_ROWS) if rows is None else list(rows)


def duck_db(settings: dict) -> float:
    db = settings.get("duckDb")
    return DEFAULT_DB if db is None else float(db)


def _length(item: dict, media: dict) -> float:
    """Mirror of audioLength in lib/sequence.js."""
    full = float(media.get("duration") or 0.0)
    t_in = float(item.get("in") or 0.0)
    t_out = full if item.get("out") is None else float(item["out"])
    return max(0.1, (t_out - t_in) / float(item.get("speed") or 1.0))


def merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sorted spans, joined where the gap between two is shorter than GAP; the dips then never overlap."""
    out: list[tuple[float, float]] = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] < GAP:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def voice_spans(project: dict, source: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Where voice plays, merged. source: spans of the picture clips whose own sound is heard (the 原聲 row).
    Empty when no row is marked 背景音樂: nothing would dip."""
    tracks = project.get("tracks") or {}
    bgm = set(tracks.get("bgmRows") or [])
    if not bgm:
        return []
    rows = set(speech_rows(tracks))
    spans: list[tuple[float, float]] = []
    for item in project.get("audio", []):
        key = audio_track(item)
        if key in bgm or not (item.get("speech") or key in rows):
            continue
        media = audio_media(project, item)
        if item.get("muted") or tracks.get("voiceMuted" if key == VOICE_TRACK else "musicMuted") or not media:
            continue
        start = float(item.get("start") or 0.0)
        spans.append((start, start + _length(item, media)))
    if SOURCE_ROW in rows and not tracks.get("videoMuted"):
        spans += source
    return merge_spans(spans)


def _dip(s: float, e: float, t: float) -> float:
    """0 to 1: how far the music is down at time t for one merged span."""
    return max(0.0, min(1.0, (t - (s - ATTACK)) / ATTACK, (e + RELEASE - t) / RELEASE))


def duck_gain(spans: list[tuple[float, float]], t: float, db: float) -> float:
    """The music's gain at timeline time t; 1 away from voice, 10^(-db/20) under it."""
    depth = 1 - 10 ** (-db / 20)
    return 1 - depth * sum(_dip(s, e, t) for s, e in spans)


def duck_expr(spans: list[tuple[float, float]], start: float, length: float, db: float) -> str | None:
    """duck_gain as an ffmpeg expression in the clip's local `t` (timeline time minus start), with only the spans
    that reach into the clip; None when none does or db is 0."""
    near = [(s, e) for s, e in spans if s - ATTACK < start + length and e + RELEASE > start]
    if not near or db <= 0:
        return None
    depth = 1 - 10 ** (-db / 20)
    tt = f"(t+{start:.3f})"
    dips = "+".join(
        f"max(0,min(min(1,({tt}-({s - ATTACK:.3f}))/{ATTACK}),({e + RELEASE:.3f}-{tt})/{RELEASE}))" for s, e in near
    )
    return f"1-{depth:.4f}*({dips})"
