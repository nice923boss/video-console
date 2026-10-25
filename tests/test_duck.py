"""AU-02: music on the rows marked 背景音樂 dips while a voice plays, and comes back after."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_server import needs_ffmpeg
from test_timeline_keys import JS, VID, make_project, plan, run_js, with_video
from vc.duck import ATTACK, RELEASE, duck_db, duck_expr, duck_gain, merge_spans, speech_rows, voice_spans
from vc.export import export_options, render
from vc.media import Media, find_ffmpeg
from vc.ops import apply_ops
from vc.util import AppError

MUSIC = "a_music0000"
LOW = 10 ** (-10 / 20)


def with_sound(p: dict) -> dict:
    """A 6 s music clip on its own row and a 1 s voice line at 2 s on the 配音 rows."""
    p["media"]["m_music000"] = {"id": "m_music000", "kind": "audio", "file": "media/takes/music.wav", "duration": 6.0}
    p["media"]["m_voice000"] = {"id": "m_voice000", "kind": "audio", "file": "media/takes/voice.wav", "duration": 1.0}
    p["audio"] = [{"id": MUSIC, "mediaId": "m_music000", "start": 0.0, "volume": 1.0},
                  {"id": "a_voice0000", "mediaId": "m_voice000", "track": "voice", "start": 2.0, "volume": 1.0}]
    p["tracks"] = {"bgmRows": [MUSIC]}
    return p


def ff_duck(text: str, t: float) -> float:
    """Evaluate the expression duck_expr writes (max, min and arithmetic in t)."""
    return eval(text, {"max": max, "min": min, "__builtins__": {}}, {"t": t})  # noqa: S307


def test_close_voices_share_one_dip() -> None:
    gap = ATTACK + RELEASE
    assert merge_spans([(5.0, 6.0), (0.0, 1.0), (1.0 + gap - 0.01, 3.0)]) == [(0.0, 3.0), (5.0, 6.0)]
    assert merge_spans([(0.0, 1.0), (1.0 + gap, 2.0)]) == [(0.0, 1.0), (1.0 + gap, 2.0)]


def test_gain_dips_before_the_voice_and_comes_back_after() -> None:
    spans = [(2.0, 3.0)]
    assert duck_gain(spans, 1.0, 10) == 1.0 and duck_gain(spans, 5.0, 10) == 1.0
    assert duck_gain(spans, 2.0, 10) == pytest.approx(LOW) and duck_gain(spans, 3.0, 10) == pytest.approx(LOW)
    assert duck_gain(spans, 2.0 - ATTACK / 2, 10) == pytest.approx((1 + LOW) / 2)
    assert duck_gain(spans, 3.0 + RELEASE / 2, 10) == pytest.approx((1 + LOW) / 2)
    assert duck_gain(spans, 2.5, 6) == pytest.approx(10 ** (-6 / 20))
    assert duck_gain(spans, 2.5, 0) == 1.0


def test_voice_is_what_the_user_marked() -> None:
    p = with_sound(make_project())
    assert voice_spans(p, []) == [(2.0, 3.0)]  # the 配音 rows count by default
    p["tracks"]["bgmRows"] = []
    assert voice_spans(p, [(0.0, 4.0)]) == []  # nothing dips: no point working it out
    p["tracks"] = {"bgmRows": [MUSIC], "speechRows": []}
    assert voice_spans(p, []) == []
    p["audio"][1]["speech"] = True  # this one clip only
    assert voice_spans(p, []) == [(2.0, 3.0)]
    p["audio"][0]["speech"] = True  # the music never ducks under itself
    assert voice_spans(p, []) == [(2.0, 3.0)]
    p["tracks"]["speechRows"] = ["source"]
    assert voice_spans(p, [(5.0, 6.0)]) == [(2.0, 3.0), (5.0, 6.0)]
    p["tracks"]["videoMuted"] = True
    assert voice_spans(p, [(5.0, 6.0)]) == [(2.0, 3.0)]
    p["audio"][1]["muted"] = True
    assert voice_spans(p, []) == []


def test_sound_effects_rows_keep_their_level(tmp_path: Path) -> None:
    p = with_sound(make_project())
    p["media"]["m_sfx00000"] = {"id": "m_sfx00000", "kind": "audio", "file": "media/takes/music.wav", "duration": 6.0}
    p["audio"].append({"id": "a_sfx000000", "mediaId": "m_sfx00000", "start": 0.0, "volume": 1.0})
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    for name in ("music.wav", "voice.wav"):
        (takes / name).write_bytes(b"")
    graph = plan(p, tmp_path)["graph"]
    music = next(line for line in graph.split(";\n") if line.endswith("[ta0]"))
    sfx = next(line for line in graph.split(";\n") if line.endswith("[ta2]"))
    assert music.count("volume=") == 2 and sfx.count("volume=") == 1
    expr = re.search(r"volume='(1-[^']+)'", music).group(1)
    for t in [i / 10 for i in range(60)]:
        assert ff_duck(expr, t) == pytest.approx(duck_gain([(2.0, 3.0)], t, 10), abs=1e-3)


def test_expression_only_carries_the_spans_near_the_clip() -> None:
    spans = [(1.0, 2.0), (10.0, 11.0)]
    assert duck_expr(spans, 4.0, 2.0, 10) is None
    assert duck_expr(spans, 0.0, 5.0, 0) is None
    one = duck_expr(spans, 9.0, 1.0, 10)
    assert one.count("max(") == 1
    for t in (0.0, 0.8, 1.0):
        assert ff_duck(one, t) == pytest.approx(duck_gain(spans, 9.0 + t, 10), abs=1e-3)
    early = duck_expr([(0.1, 1.0)], 0.0, 2.0, 10)  # the dip starts before 0: no "--" in the expression
    assert "--" not in early and ff_duck(early, 0.0) == pytest.approx(duck_gain([(0.1, 1.0)], 0.0, 10), abs=1e-3)


def test_row_roles_and_depth_validate_and_undo() -> None:
    p = make_project()
    inverse = apply_ops(p, [{"op": "project.update", "patch": {
        "tracks": {"speechRows": ["voice", "source", MUSIC], "bgmRows": ["a_other0000"]},
        "settings": {"duckDb": 8}}}], {})
    assert speech_rows(p["tracks"]) == ["voice", "source", MUSIC] and duck_db(p["settings"]) == 8.0
    apply_ops(p, inverse, {})
    assert speech_rows(p["tracks"]) == ["voice"] and not p["tracks"].get("bgmRows")
    assert duck_db(p["settings"]) == 10.0
    for patch in ({"tracks": {"bgmRows": "voice"}}, {"tracks": {"speechRows": ["Not An Id"]}},
                  {"settings": {"duckDb": "loud"}}):
        with pytest.raises(AppError):
            apply_ops(p, [{"op": "project.update", "patch": patch}], {})
    apply_ops(p, [{"op": "project.update", "patch": {"settings": {"duckDb": 30}}}], {})
    assert duck_db(p["settings"]) == 24.0  # numbers clamp, like every other setting
    p["audio"] = [{"id": MUSIC, "mediaId": "m_music000", "start": 0.0}]
    inverse = apply_ops(p, [{"op": "audio.update", "id": MUSIC, "patch": {"speech": True}}], {})
    assert p["audio"][0]["speech"] is True
    apply_ops(p, inverse, {})
    assert not p["audio"][0].get("speech")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_preview_ducks_like_the_export() -> None:
    p = with_sound(with_video(make_project()))
    p["tracks"]["speechRows"] = ["voice", "source"]
    p["audio"][1]["speed"] = 2.0
    source = [(0.0, 1.0), (6.0, 7.5)]
    times = [i / 10 for i in range(90)]
    got = run_js(JS / "lib" / "duck.js", """const spans = m.voiceSpans(data.p, data.source);
      console.log(JSON.stringify({ spans, gain: data.times.map((t) => m.duckGain(spans, t, 8)),
        roles: [m.rowRole(data.p.tracks, "voice"), m.rowRole(data.p.tracks, data.music), m.rowRole(data.p.tracks, "x")],
        op: m.roleOp(data.p.tracks, "voice", "bgm") }));""",
                 {"p": p, "source": source, "times": times, "music": MUSIC})
    spans = voice_spans(p, source)
    assert [tuple(s) for s in got["spans"]] == pytest.approx(spans)
    assert got["gain"] == pytest.approx([duck_gain(spans, t, 8) for t in times])
    assert got["roles"] == ["speech", "bgm", ""]
    assert got["op"]["patch"]["tracks"] == {"speechRows": ["source"], "bgmRows": [MUSIC, "voice"]}


@needs_ffmpeg
def test_render_lowers_the_music_under_the_voice(tmp_path: Path) -> None:
    """A loud tone as music and a silent voice line from 2 s to 3 s: the mix there is 10 dB lower."""
    ffmpeg = find_ffmpeg()
    media = Media(ffmpeg)
    takes = tmp_path / "media" / "takes"
    takes.mkdir(parents=True)
    media.run(["-f", "lavfi", "-i", "sine=frequency=440:duration=6", str(takes / "music.wav")])
    media.run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", str(takes / "voice.wav")])
    p = with_sound(make_project())
    out = tmp_path / render(media, p, tmp_path, export_options(p, {}, {"format": "wav"}))["file"]

    def mean_db(start: float) -> float:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-ss", str(start), "-t", "0.5", "-i", str(out),
                               "-af", "volumedetect", "-f", "null", "-"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        return float(re.search(r"mean_volume:\s+(-?[\d.]+) dB", proc.stderr).group(1))

    before, under, after = mean_db(0.5), mean_db(2.25), mean_db(4.5)
    assert before - under == pytest.approx(10.0, abs=0.5)
    assert after == pytest.approx(before, abs=0.3)
